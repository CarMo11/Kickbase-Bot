#!/usr/bin/env python3
import os
import logging
from typing import Any, Dict, List

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

BASE_URL = "https://api.kickbase.com"

LOGIN_URL = f"{BASE_URL}/v4/user/login"
LEAGUE_ME_URL_TEMPLATE = BASE_URL + "/v4/league/{league_id}/me"
MARKET_URL_TEMPLATE = BASE_URL + "/v4/market/league/{league_id}"

# ⚠️ Offer-Endpoint: dieser Pfad ist der wahrscheinlich richtige.
# Falls du weiterhin 404 bekommst, muss GENAU diese URL an den echten
# Kickbase-Endpunkt angepasst werden (der Rest des Bots ist davon unabhängig).
OFFER_URL_TEMPLATE = BASE_URL + "/v4/market/league/{league_id}/player/{player_id}/offer"

BASE_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "KickbaseBot/0.1 (+github.com/CarMo11/Kickbase-Bot)",
}

# Wie stark überbieten wir den Marktwert?
BID_OFFSET = 2

# Nur Spieler mit positivem Trend mvt in dieser Liste werden berücksichtigt
ALLOWED_TREND_FLAGS = {1, 2}


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def getenv_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "y", "on")


# ---------------------------------------------------------------------------
# Kickbase HTTP-Funktionen (v4)
# ---------------------------------------------------------------------------

def login_v4(session: requests.Session, email: str, password: str) -> None:
    """
    Führt den Kickbase-Login über /v4/user/login aus und setzt das Auth-Token
    im Session-Header.
    """
    logging.info("Versuche Kickbase Login über /v4/user/login ...")

    session.headers.clear()
    session.headers.update(BASE_HEADERS)

    payload = {
        "email": email,
        "password": password,
    }

    r = session.post(LOGIN_URL, json=payload, timeout=15)
    logging.info("Login-HTTP-Status: %s", r.status_code)

    if r.status_code != 200:
        # Kickbase schickt z.B. {"err":1,"errMsg":"AccessDenied","svcs":[]}
        raise RuntimeError(f"Login fehlgeschlagen (Status {r.status_code}): {r.text}")

    # Auth-Token aus Header holen
    token = r.headers.get("x-auth-token")
    if not token:
        raise RuntimeError("Login erfolgreich, aber kein 'x-auth-token' im Header gefunden.")

    session.headers["x-auth-token"] = token

    try:
        data = r.json()
    except Exception:
        data = {}

    username = data.get("un") or data.get("username") or "Unbekannt"
    logging.info("Login erfolgreich. Eingeloggt als: %s", username)


def get_league_me(session: requests.Session, league_id: str) -> Dict[str, Any]:
    url = LEAGUE_ME_URL_TEMPLATE.format(league_id=league_id)
    logging.info("Hole league_me JSON für Liga %s ...", league_id)

    r = session.get(url, timeout=15)
    logging.info("league_me Status: %s", r.status_code)
    r.raise_for_status()

    data = r.json()
    logging.info("league_me raw JSON: %s", data)
    return data


def get_market(session: requests.Session, league_id: str) -> Dict[str, Any]:
    url = MARKET_URL_TEMPLATE.format(league_id=league_id)
    logging.info("Hole market JSON für Liga %s ...", league_id)

    r = session.get(url, timeout=15)
    logging.info("market Status: %s", r.status_code)
    r.raise_for_status()

    data = r.json()
    logging.info("market raw JSON: %s", data)
    return data


def make_offer_v4(
    session: requests.Session,
    league_id: str,
    player_id: str,
    price: int,
) -> None:
    """
    Schickt ein Gebot an den (vermuteten) v4-Offer-Endpunkt.

    Falls hier weiterhin 404 kommt, musst du nur diese Funktion anpassen:
    - URL in OFFER_URL_TEMPLATE
    - evtl. Payload-Format
    """
    url = OFFER_URL_TEMPLATE.format(league_id=league_id, player_id=player_id)
    payload = {"price": price}

    logging.info(
        "make_offer_v4: league_id=%s, player_id=%s, price=%s",
        league_id,
        player_id,
        price,
    )

    r = session.post(url, json=payload, timeout=15)
    if r.status_code != 200:
        logging.error(
            "make_offer_v4 fehlgeschlagen: Status=%s, body=%s",
            r.status_code,
            r.text,
        )
        raise RuntimeError(f"make_offer_v4 failed with status {r.status_code}")
    else:
        logging.info("make_offer_v4 erfolgreich: Status=%s", r.status_code)


# ---------------------------------------------------------------------------
# Markt-Logik
# ---------------------------------------------------------------------------

def select_candidates(market_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = market_json.get("it", [])
    logging.info("Spieler auf dem Markt (JSON 'it'): %d", len(items))

    candidates: List[Dict[str, Any]] = []

    for item in items:
        try:
            player_id = str(item.get("i"))
            first_name = (item.get("fn") or "").strip()
            last_name = (item.get("n") or "").strip()
            name = (first_name + " " + last_name).strip() or f"Player {player_id}"

            mv = int(item.get("mv", 0))  # Marktwert
            mvt = int(item.get("mvt", 0))  # Trendflag
            exs = int(item.get("exs", 0))  # Sekunden / sonstiger Counter
            rest_min = exs // 60

            if mvt not in ALLOWED_TREND_FLAGS:
                # Nur positive Trends
                continue

            # Gebot = Marktwert + Offset
            bid_price = mv + BID_OFFSET if mv > 0 else int(item.get("prc", 0)) + BID_OFFSET

            candidates.append(
                {
                    "player_id": player_id,
                    "name": name,
                    "mv": mv,
                    "mvt": mvt,
                    "rest_min": rest_min,
                    "bid_price": bid_price,
                }
            )
        except Exception as e:
            logging.error("Fehler beim Verarbeiten eines Markt-Eintrags: %s", e)

    # Optional: Sortieren – z.B. nach Trendflag und Marktwert
    candidates.sort(key=lambda c: (-c["mvt"], -c["mv"]))
    return candidates


def process_market(
    session: requests.Session,
    league_id: str,
    budget: int,
    dry_run: bool,
) -> None:
    market_json = get_market(session, league_id)

    items = market_json.get("it", [])
    logging.info("Spieler auf dem Markt (JSON 'it'): %d", len(items))

    candidates = select_candidates(market_json)

    if not candidates:
        logging.info("Keine geeigneten Kandidaten (positiver Trend) gefunden.")
        return

    remaining_budget = budget
    logging.info("Starte Auto-Bieten mit Budget: %d", remaining_budget)

    for cand in candidates:
        name = cand["name"]
        mv = cand["mv"]
        mvt = cand["mvt"]
        rest_min = cand["rest_min"]
        bid_price = cand["bid_price"]
        player_id = cand["player_id"]

        logging.info(
            "Kandidat: %s | MW=%d | TrendFlag=%d | Rest=%d min | Gebot=%d",
            name,
            mv,
            mvt,
            rest_min,
            bid_price,
        )

        if bid_price > remaining_budget:
            logging.info(
                "Überspringe %s – Gebot %d > verbleibendes Budget %d",
                name,
                bid_price,
                remaining_budget,
            )
            continue

        if dry_run:
            logging.info(
                "[DRY_RUN] Würde Gebot %d für %s (player_id=%s, Liga=%s) senden.",
                bid_price,
                name,
                player_id,
                league_id,
            )
            remaining_budget -= bid_price  # Für Simulation Budget abziehen
            continue

        try:
            logging.info(
                "Sende Gebot %d für %s (player_id=%s, Liga=%s)...",
                bid_price,
                name,
                player_id,
                league_id,
            )
            make_offer_v4(session, league_id, player_id, bid_price)
            remaining_budget -= bid_price
        except Exception as e:
            logging.error(
                "make_offer fehlgeschlagen für %s (player_id=%s): %s",
                name,
                player_id,
                e,
            )

    logging.info("Bot-Durchlauf fertig. Verbleibendes Budget (theoretisch): %d", remaining_budget)


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

def main() -> None:
    setup_logging()

    load_dotenv()  # Lokal praktisch, in GitHub Actions egal

    email = os.getenv("KICKBASE_EMAIL")
    password = os.getenv("KICKBASE_PASSWORD")
    league_id = os.getenv("KICKBASE_LEAGUE_ID")

    dry_run = getenv_bool("KICKBASE_DRY_RUN", False)

    if not email or not password or not league_id:
        raise RuntimeError(
            "Bitte Umgebungsvariablen KICKBASE_EMAIL, KICKBASE_PASSWORD "
            "und KICKBASE_LEAGUE_ID setzen."
        )

    logging.info("Starte Kickbase-Bot (DRY_RUN=%s)...", dry_run)

    with requests.Session() as session:
        # Login
        login_v4(session, email, password)

        # Budget / League-Me
        league_me = get_league_me(session, league_id)
        budget_raw = league_me.get("b", 0)
        try:
            budget = int(budget_raw)
        except Exception:
            budget = int(float(budget_raw) or 0)

        logging.info("Budget (aus JSON 'b'): %d", budget)

        # Markt abarbeiten
        process_market(session, league_id, budget, dry_run)


if __name__ == "__main__":
    main()
