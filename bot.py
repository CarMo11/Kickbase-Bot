import os
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any

import requests
from dotenv import load_dotenv

# --------------------------------------------------------
# Logging
# --------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

log = logging.getLogger("kickbase-bot")


# --------------------------------------------------------
# Config
# --------------------------------------------------------
load_dotenv()

EMAIL = os.getenv("KICKBASE_EMAIL")
PASSWORD = os.getenv("KICKBASE_PASSWORD")
LEAGUE_ID = os.getenv("KICKBASE_LEAGUE_ID")

# DRY_RUN=True  -> nur Log-Ausgabe, keine echten Gebote
# DRY_RUN=False -> echte Gebote
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

BASE_URL = "https://api.kickbase.com"


# --------------------------------------------------------
# HTTP / Login
# --------------------------------------------------------
def login_v4(session: requests.Session, email: str, password: str) -> str:
    """
    Loggt sich über die v4-Login-Route ein und gibt das Token zurück.
    Setzt außerdem den Authorization-Header im Session-Objekt.
    """
    url = f"{BASE_URL}/v4/user/login"
    payload = {"email": email, "password": password}

    log.info("Versuche Kickbase Login über %s ...", url)
    r = session.post(url, json=payload)
    log.info("Login-HTTP-Status: %s", r.status_code)

    if r.status_code != 200:
        raise RuntimeError(f"Login fehlgeschlagen (Status {r.status_code}): {r.text}")

    data = r.json()
    # Token-Feld robust auslesen (je nach API-Version)
    token = data.get("token") or data.get("t")
    if not token:
        raise RuntimeError("Login erfolgreich, aber kein Token im Response gefunden")

    user = data.get("user") or data.get("u") or {}
    username = user.get("n") or user.get("name") or "Unbekannt"

    # Authorization-Header für weitere Requests
    session.headers.update({"authorization": f"Basic {token}"})

    log.info("Login erfolgreich. Eingeloggt als: %s", username)
    log.info("Eingeloggt als %s", username)
    return token


def get_league_me(session: requests.Session, league_id: str) -> Dict[str, Any]:
    url = f"{BASE_URL}/v4/leagues/{league_id}/me"
    log.info("Hole league_me JSON für Liga %s ...", league_id)
    r = session.get(url)
    log.info("league_me Status: %s", r.status_code)
    r.raise_for_status()
    data = r.json()
    log.info("league_me raw JSON: %s", data)
    return data


def get_market(session: requests.Session, league_id: str) -> Dict[str, Any]:
    url = f"{BASE_URL}/v4/leagues/{league_id}/market"
    log.info("Hole market JSON für Liga %s ...", league_id)
    r = session.get(url)
    log.info("market Status: %s", r.status_code)
    r.raise_for_status()
    data = r.json()
    log.info("market raw JSON: %s", data)
    return data


# --------------------------------------------------------
# Offer / Gebot senden
# --------------------------------------------------------
def try_send_offer(
    session: requests.Session, league_id: str, player_id: str, price: int
) -> bool:
    """
    Versucht, ein Gebot über mehrere mögliche Endpoints/Methoden zu senden.
    Gibt True zurück, wenn irgendein Versuch Erfolg hatte.
    """

    # Mögliche Pfade – da Kickbase hier wohl umgebaut hat, probieren wir ein paar Varianten.
    candidate_paths = [
        f"/v4/leagues/{league_id}/market/{player_id}/offer",
        f"/v4/leagues/{league_id}/market/{player_id}/bid",
        f"/leagues/{league_id}/market/{player_id}/offer",
        f"/leagues/{league_id}/transfermarket/{player_id}/offer",
    ]
    methods = ("post", "put")

    payload = {"price": price}

    for path in candidate_paths:
        url = BASE_URL + path
        for method in methods:
            log.info("make_offer: versuche %s %s mit price=%s", method.upper(), url, price)
            req = getattr(session, method)
            r = req(url, json=payload)

            status = r.status_code
            body = r.text
            log.info("make_offer: Status=%s, Body=%s", status, body[:300])

            if status in (200, 201, 204):
                log.info("Gebot erfolgreich über %s %s gesendet.", method.upper(), url)
                return True

            # 4xx = Pfad/Methoden-Kombi passt wohl nicht -> nächste probieren
            if 400 <= status < 500:
                continue

    return False


def send_offer(
    session: requests.Session,
    league_id: str,
    player_id: str,
    player_name: str,
    bid_price: int,
) -> None:
    if DRY_RUN:
        log.info(
            "[DRY_RUN] Würde Gebot %s für %s (player_id=%s, Liga=%s) senden.",
            bid_price,
            player_name,
            player_id,
            league_id,
        )
        return

    log.info(
        "Sende Gebot %s für %s (player_id=%s, Liga=%s)...",
        bid_price,
        player_name,
        player_id,
        league_id,
    )

    ok = try_send_offer(session, league_id, str(player_id), bid_price)
    if not ok:
        log.error(
            "make_offer fehlgeschlagen für %s (player_id=%s) – keine der getesteten Routen hat funktioniert.",
            player_name,
            player_id,
        )


# --------------------------------------------------------
# Auswahl der Kandidaten
# --------------------------------------------------------
def select_candidates(
    market_data: Dict[str, Any],
    budget: int,
    max_players: int = 5,
) -> List[Dict[str, Any]]:
    """
    Wählt Markt-Spieler aus, für die wir bieten wollen.
    Nutzt direkt das JSON (Feld 'it'), wie es im Log zu sehen ist.
    """
    items = market_data.get("it", [])
    log.info("Spieler auf dem Markt (JSON 'it'): %s", len(items))

    # einfache Filter-Parameter – kannst du nach Belieben anpassen
    min_trend_flag = 1          # mvt >= 1 (steigende Tendenz)
    min_rest_minutes = 120      # mind. 2 Stunden Restzeit
    max_candidates = max_players

    candidates: List[Dict[str, Any]] = []

    for item in items:
        player_id = item.get("i")
        first_name = item.get("fn", "") or ""
        last_name = item.get("n", "") or ""
        name = (first_name + " " + last_name).strip()

        mv = int(item.get("mv", 0))          # Marktwert
        trend_flag = int(item.get("mvt", 0)) # 0,1,2...
        exs = int(item.get("exs", 0))        # Restzeit in Sekunden

        rest_minutes = exs // 60

        # einfacher Filter
        if trend_flag < min_trend_flag:
            continue
        if rest_minutes < min_rest_minutes:
            continue
        if mv <= 0 or mv > budget:
            continue

        # Beispiel-Gebot: Marktwert + 1
        bid_price = mv + 1

        log.info(
            "Kandidat: %s | MW=%s | TrendFlag=%s | Rest=%s min | Gebot=%s",
            name,
            mv,
            trend_flag,
            rest_minutes,
            bid_price,
        )

        candidates.append(
            {
                "player_id": player_id,
                "name": name,
                "mv": mv,
                "trend_flag": trend_flag,
                "rest_minutes": rest_minutes,
                "bid_price": bid_price,
            }
        )

        if len(candidates) >= max_candidates:
            break

    return candidates


# --------------------------------------------------------
# Main
# --------------------------------------------------------
def main() -> None:
    if not EMAIL or not PASSWORD or not LEAGUE_ID:
        raise SystemExit(
            "Bitte KICKBASE_EMAIL, KICKBASE_PASSWORD und KICKBASE_LEAGUE_ID in .env setzen!"
        )

    log.info("Starte Kickbase-Bot (DRY_RUN=%s)...", DRY_RUN)

    with requests.Session() as session:
        # Login
        login_v4(session, EMAIL, PASSWORD)

        # Liga-Infos
        league_me = get_league_me(session, LEAGUE_ID)
        budget = int(league_me.get("b", 0))
        log.info("Budget (aus JSON 'b'): %s", budget)
        log.info("Budget: %s", budget)

        # Markt
        market_data = get_market(session, LEAGUE_ID)

        # Kandidaten wählen
        candidates = select_candidates(market_data, budget, max_players=5)

        if not candidates:
            log.info("Keine passenden Kandidaten gefunden.")
        else:
            for cand in candidates:
                send_offer(
                    session=session,
                    league_id=LEAGUE_ID,
                    player_id=cand["player_id"],
                    player_name=cand["name"],
                    bid_price=cand["bid_price"],
                )

        log.info("Bot-Durchlauf fertig.")


if __name__ == "__main__":
    main()
