import os
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

BASE_URL = "https://api.kickbase.com"
API_V4 = f"{BASE_URL}/v4"

# Wie viel Aufschlag auf den Marktwert (in €)
OFFER_BONUS = 2

# Minimaler Marktwert, damit der Bot überhaupt bietet
MIN_MARKET_VALUE = 500_000

# Logging-Konfiguration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# ---------------------------------------------------------------------------
# Hilfsfunktionen HTTP / API
# ---------------------------------------------------------------------------


def create_session() -> requests.Session:
    """
    Erzeugt eine Requests-Session mit Standard-Headern.
    """
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Kickbase-Bot/1.0 (GitHub Actions)",
        }
    )
    return session


def login_v4(session: requests.Session, email: str, password: str) -> Dict[str, Any]:
    """
    Login über die offizielle v4-Endpoint:
        POST /v4/user/login
    """
    url = f"{API_V4}/user/login"

    # zur Sicherheit evtl. Whitespaces entfernen
    email = (email or "").strip()
    password = (password or "").strip()

    logging.info("Starte Kickbase-Bot (DRY_RUN=False)...")
    logging.info("Versuche Kickbase Login über /v4/user/login ...")

    payload = {
        "email": email,
        "password": password,
    }

    r = session.post(url, json=payload)
    logging.info("Login-HTTP-Status: %s", r.status_code)

    if r.status_code != 200:
        # Wichtig: Bei 401 ist das ein Server-Problem (Credentials / Block / etc.)
        raise RuntimeError(f"Login fehlgeschlagen (Status {r.status_code}): {r.text}")

    data = r.json()

    # Username o.ä. ausgeben, Felder können leicht variieren, daher vorsichtig
    username = (
        data.get("un")
        or data.get("username")
        or data.get("n")
        or "<unbekannt>"
    )
    logging.info("Login erfolgreich. Eingeloggt als: %s", username)

    # Token in die Session-Header packen (Feldname kann je nach API leicht variieren)
    token = data.get("t") or data.get("token")
    if token:
        session.headers["Authorization"] = token

    return data


def get_leagues_v4(session: requests.Session) -> List[Dict[str, Any]]:
    """
    Holt Ligen des eingeloggten Users.
    Häufiger Endpoint:
        GET /v4/user/leagues   (falls Kickbase etwas anderes nutzt, würde hier 404 kommen)
    """
    url = f"{API_V4}/user/leagues"
    r = session.get(url)
    logging.info("Leagues-HTTP-Status: %s", r.status_code)

    if r.status_code != 200:
        raise RuntimeError(f"Leagues-Request fehlgeschlagen (Status {r.status_code}): {r.text}")

    data = r.json()
    # angenommen: {"lgs": [...]} oder {"leagues": [...]}
    leagues = data.get("lgs") or data.get("leagues") or []
    if not leagues:
        logging.info("Keine Ligen gefunden.")
    return leagues


def league_me_v4(session: requests.Session, league_id: str) -> Dict[str, Any]:
    """
    Ruft league_me-Infos (z.B. Budget) für eine Liga ab.
        GET /v4/league/{league_id}/me
    """
    url = f"{API_V4}/league/{league_id}/me"
    logging.info("Hole league_me JSON für Liga %s ...", league_id)
    r = session.get(url)
    logging.info("league_me Status: %s", r.status_code)

    if r.status_code != 200:
        raise RuntimeError(f"league_me fehlgeschlagen (Status {r.status_code}): {r.text}")

    data = r.json()
    logging.info("league_me raw JSON: %s", data)

    budget = data.get("b")
    if budget is not None:
        logging.info("Budget (aus JSON 'b'): %s", int(budget))
    return data


def market_v4(session: requests.Session, league_id: str) -> Dict[str, Any]:
    """
    Holt den Transfermarkt einer Liga.
        GET /v4/league/{league_id}/transfermarket
    (Der genaue Pfad basiert auf dem, was vorher bei dir funktioniert hat.)
    """
    url = f"{API_V4}/league/{league_id}/transfermarket"
    logging.info("Hole market JSON für Liga %s ...", league_id)
    r = session.get(url)
    logging.info("market Status: %s", r.status_code)

    if r.status_code != 200:
        raise RuntimeError(f"market fehlgeschlagen (Status {r.status_code}): {r.text}")

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
    Schickt ein Gebot für einen Spieler.
    Achtung: Der genaue Endpoint ist NICHT garantiert, weil die Kickbase API nicht offiziell ist.
    Wenn hier 404 kommt, liegt es sehr wahrscheinlich an einem leicht anderen Pfadnamen.
    """
    # Dieser Pfad war in vielen Bots gängig – wenn 404, muss man ihn anpassen
    url = f"{API_V4}/league/{league_id}/transfermarket/player/{player_id}/offer"

    logging.info(
        "make_offer_v4: league_id=%s, player_id=%s, price=%s",
        league_id,
        player_id,
        price,
    )

    payload = {"price": price}

    r = session.post(url, json=payload)

    if r.status_code != 200:
        logging.error("make_offer_v4 fehlgeschlagen: Status=%s, body=%s", r.status_code, r.text)
        raise RuntimeError(f"make_offer_v4 failed with status {r.status_code}")
    else:
        logging.info("Gebot erfolgreich gesendet (Status 200).")


# ---------------------------------------------------------------------------
# Markt-Analyse & Kandidaten
# ---------------------------------------------------------------------------


def compute_rest_minutes(market_json: Dict[str, Any]) -> int:
    """
    Berechnet die Restzeit bis Marktupdate auf Basis des Feldes 'mvud' (Market Value Update Date).
    In deinen Logs war 'mvud' z.B. '2025-12-09T21:00:00Z'.
    """
    mvud_str = market_json.get("mvud")
    if not mvud_str:
        return -1

    try:
        # Format: 2025-12-09T21:00:00Z
        mvud_dt = datetime.strptime(mvud_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        diff_min = int((mvud_dt - now).total_seconds() / 60)
        return max(diff_min, 0)
    except Exception:
        return -1


def parse_market_players(market_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Extrahiert alle Spieleromarkt-Einträge aus dem market JSON.
    Bei dir war das Feld 'it' (items).
    """
    players = market_json.get("it") or []
    logging.info("Spieler auf dem Markt (JSON 'it'): %s", len(players))
    return players


def is_candidate(player: Dict[str, Any]) -> bool:
    """
    Entscheidet, ob ein Spieler ein Kandidat zum Bieten ist.
    Hier kannst du deine Logik anpassen.
    Aktuell:
        - Marktwert >= MIN_MARKET_VALUE
        - Trendflag (mvt) > 0 (steigender oder guter Trend)
    """
    mv = player.get("mv", 0)  # Marktwert
    trend_flag = player.get("mvt", 0)

    if mv < MIN_MARKET_VALUE:
        return False

    if trend_flag <= 0:
        return False

    return True


def player_display_name(player: Dict[str, Any]) -> str:
    """
    Baut einen hübschen Namen wie 'Jeanuël Belocian' zusammen.
    """
    fn = (player.get("fn") or "").strip()
    n = (player.get("n") or "").strip()
    if fn and n:
        return f"{fn} {n}"
    return fn or n or f"Player {player.get('i')}"


def analyze_market_and_bid(
    session: requests.Session,
    league_id: str,
    market_json: Dict[str, Any],
    budget: float,
) -> None:
    """
    Nimmt das Markt-JSON, filtert Kandidaten und sendet automatisch Gebote.
    """
    players = parse_market_players(market_json)
    if not players:
        logging.info("Keine Spieler auf dem Markt.")
        return

    rest_all = compute_rest_minutes(market_json)

    for p in players:
        if not is_candidate(p):
            continue

        player_id = str(p.get("i"))
        name = player_display_name(p)
        mv = int(p.get("mv", 0))
        trend_flag = p.get("mvt", 0)

        # Wenn wir uns das überhaupt leisten können
        if mv + OFFER_BONUS > budget:
            logging.info(
                "Überspringe %s (ID=%s), da Gebot %s > Budget %s",
                name,
                player_id,
                mv + OFFER_BONUS,
                int(budget),
            )
            continue

        bid_price = mv + OFFER_BONUS

        logging.info(
            "Kandidat: %s | MW=%s | TrendFlag=%s | Rest=%s min | Gebot=%s",
            name,
            mv,
            trend_flag,
            rest_all,
            bid_price,
        )

        try:
            make_offer_v4(session, league_id, player_id, bid_price)
        except Exception as e:
            logging.error(
                "make_offer fehlgeschlagen für %s (player_id=%s): %s",
                name,
                player_id,
                e,
            )


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def main() -> None:
    # .env nur für lokale Nutzung; in GitHub Actions kommen die Werte aus Secrets
    load_dotenv()

    email = os.getenv("KICKBASE_EMAIL")
    password = os.getenv("KICKBASE_PASSWORD")
    league_id_env = os.getenv("KICKBASE_LEAGUE_ID")

    if not email or not password:
        raise RuntimeError("KICKBASE_EMAIL oder KICKBASE_PASSWORD ist nicht gesetzt.")

    session = create_session()

    # Login
    login_data = login_v4(session, email, password)

    # Ligen holen & gewünschte Liga bestimmen
    leagues = get_leagues_v4(session)

    if not leagues:
        logging.info("Keine Ligen verfügbar, Bot beendet sich.")
        return

    chosen_league_id = None

    # Falls explizit eine Liga-ID gesetzt ist, nimm diese
    if league_id_env:
        for lg in leagues:
            if str(lg.get("id")) == str(league_id_env) or str(lg.get("lid")) == str(league_id_env):
                chosen_league_id = str(league_id_env)
                logging.info(
                    "Nutze Liga (per ENV): %s (ID=%s)",
                    lg.get("lnm") or lg.get("name") or "<ohne Name>",
                    chosen_league_id,
                )
                break

    # Wenn keine ENV-Liga gefunden wurde, nimm einfach die erste
    if not chosen_league_id:
        first = leagues[0]
        chosen_league_id = str(first.get("id") or first.get("lid"))
        logging.info(
            "Nutze Liga (erste gefundene): %s (ID=%s)",
            first.get("lnm") or first.get("name") or "<ohne Name>",
            chosen_league_id,
        )

    # league_me -> Budget
    league_me = league_me_v4(session, chosen_league_id)
    budget = float(league_me.get("b", 0))
    logging.info("Budget: %s", int(budget))

    # Markt holen
    market_json = market_v4(session, chosen_league_id)

    # Markt analysieren & Gebote abgeben
    analyze_market_and_bid(session, chosen_league_id, market_json, budget)

    logging.info("Bot-Durchlauf fertig.")


if __name__ == "__main__":
    main()
