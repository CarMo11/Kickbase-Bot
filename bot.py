import os
import logging
from datetime import datetime, timezone
import requests
from dotenv import load_dotenv

# Basis-URL der Kickbase API
BASE_URL = "https://api.kickbase.com"

# Standard-Header – eher konservativ halten
BASE_HEADERS = {
    "User-Agent": "KickbaseBot/1.0",
    "Accept": "application/json",
    "Connection": "keep-alive",
}

REQUEST_TIMEOUT = 10  # Sekunden


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def get_env_credentials():
    """
    Liest die Logindaten und Liga-ID aus ENV Variablen.
    Erwartet:
      - KICKBASE_EMAIL
      - KICKBASE_PASSWORD
      - KICKBASE_LEAGUE_ID
      - optional: KICKBASE_DRY_RUN = 'true' / 'false'
    """
    load_dotenv()

    email = os.environ.get("KICKBASE_EMAIL")
    password = os.environ.get("KICKBASE_PASSWORD")
    league_id = os.environ.get("KICKBASE_LEAGUE_ID")

    if not email or not password:
        raise RuntimeError("KICKBASE_EMAIL oder KICKBASE_PASSWORD fehlt in den Umgebungsvariablen.")

    if not league_id:
        raise RuntimeError("KICKBASE_LEAGUE_ID fehlt in den Umgebungsvariablen.")

    dry_run_raw = os.environ.get("KICKBASE_DRY_RUN", "false").lower()
    dry_run = dry_run_raw in ("1", "true", "yes", "y")

    return email, password, league_id, dry_run


# ------------------------------------------------------------
# Login & API-Calls (v4)
# ------------------------------------------------------------

def login_v4(session: requests.Session, email: str, password: str) -> dict:
    """
    Führt einen Login gegen /v4/user/login aus.
    Nutzt ein simples JSON mit email/password – das war zuvor bei dir erfolgreich.
    """
    url = f"{BASE_URL}/v4/user/login"
    headers = {**BASE_HEADERS}

    payload = {
        "email": email,
        "password": password,
    }

    logging.info("Versuche Kickbase Login über /v4/user/login ...")
    r = session.post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
    logging.info("Login-HTTP-Status: %s", r.status_code)

    if r.status_code != 200:
        # Hier kam bei dir zuletzt: {"err":1,"errMsg":"AccessDenied","svcs":[]}
        raise RuntimeError(f"Login fehlgeschlagen (Status {r.status_code}): {r.text}")

    data = r.json()
    # Versuche, einen Namen / Username auszugeben – Feldname kann variieren
    username = data.get("un") or data.get("name") or data.get("n")
    if username:
        logging.info("Login erfolgreich. Eingeloggt als: %s", username)
    else:
        logging.info("Login erfolgreich.")

    return data


def get_league_me_v4(session: requests.Session, league_id: str) -> dict:
    """
    Ruft die League-Me-Infos für eine Liga ab.
    """
    url = f"{BASE_URL}/v4/league/{league_id}/me"
    headers = {**BASE_HEADERS, "X-L-Id": str(league_id)}

    logging.info("Hole league_me JSON für Liga %s ...", league_id)
    r = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    logging.info("league_me Status: %s", r.status_code)
    r.raise_for_status()
    data = r.json()
    logging.info("league_me raw JSON: %s", data)
    return data


def get_market_v4(session: requests.Session, league_id: str) -> dict:
    """
    Ruft den Markt für eine Liga ab.
    """
    url = f"{BASE_URL}/v4/league/{league_id}/market"
    headers = {**BASE_HEADERS, "X-L-Id": str(league_id)}

    logging.info("Hole market JSON für Liga %s ...", league_id)
    r = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    logging.info("market Status: %s", r.status_code)
    r.raise_for_status()
    data = r.json()
    logging.info("market raw JSON: %s", data)
    return data


def calc_rest_min(item: dict) -> int | None:
    """
    Berechnet die Restzeit auf dem Markt in Minuten.
    In deinen Logs passte das gut zu exs/60 -> 160, 400, 640, ...
    """
    exs = item.get("exs")
    if isinstance(exs, (int, float)):
        return int(exs // 60)
    return None


def make_offer_v4(
    session: requests.Session,
    league_id: str,
    player_id: str,
    price: int,
    dry_run: bool = False,
) -> None:
    """
    Sendet ein Gebot auf einen Spieler.
    ACHTUNG: Der genaue Endpoint ist inoffiziell; falls hier weiterhin 404 kommt,
    muss evtl. noch am Pfad / Body nachjustiert werden.
    """
    headers = {**BASE_HEADERS, "X-L-Id": str(league_id)}

    # Kandidat für v4-Endpoint: /v4/league/{league_id}/market/{player_id}
    url = f"{BASE_URL}/v4/league/{league_id}/market/{player_id}"

    logging.info(
        "make_offer_v4: league_id=%s, player_id=%s, price=%s",
        league_id,
        player_id,
        price,
    )

    if dry_run:
        logging.info("DRY_RUN=True – Gebot NICHT gesendet.")
        return

    payload = {
        "price": price
    }

    r = session.post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)

    if r.status_code != 200:
        logging.error(
            "make_offer_v4 fehlgeschlagen: Status=%s, body=%s",
            r.status_code,
            r.text,
        )
        # Kein raise_for_status(), damit der Bot weiter andere Spieler versuchen kann.
        return

    logging.info(
        "Gebot erfolgreich gesendet (Status=%s). Antwort: %s",
        r.status_code,
        r.text,
    )


# ------------------------------------------------------------
# Biet-Logik
# ------------------------------------------------------------

def select_and_bid(session: requests.Session, league_id: str, market: dict, budget: int, dry_run: bool) -> None:
    """
    Geht alle Marktspieler durch, wählt Kandidaten aus und sendet Gebote.
    Aktuell:
      - Trendflag (mvt) muss 1 oder 2 sein (positiver Trend)
      - Marktwert darf Budget nicht überschreiten
      - Bid = MW + 2 (sehr simpel)
    """

    items = market.get("it", []) or []
    logging.info("Spieler auf dem Markt (JSON 'it'): %s", len(items))

    if not items:
        logging.info("Keine Spieler auf dem Markt.")
        return

    for item in items:
        player_id = str(item.get("i"))
        fn = (item.get("fn") or "").strip()
        ln = (item.get("n") or "").strip()
        name = f"{fn} {ln}".strip() or f"Player {player_id}"

        mv = int(item.get("mv") or 0)
        trend = item.get("mvt", 0)
        rest_min = calc_rest_min(item)

        # Nur positive Trends
        if trend not in (1, 2):
            continue

        # Kein Gebot über Budget
        if mv <= 0 or mv > budget:
            continue

        if rest_min is None:
            rest_min = -1

        # Sehr simple Bietstrategie: Marktwert + 2
        bid = mv + 2

        logging.info(
            "Kandidat: %s | MW=%s | TrendFlag=%s | Rest=%s min | Gebot=%s",
            name,
            mv,
            trend,
            rest_min,
            bid,
        )

        make_offer_v4(
            session=session,
            league_id=league_id,
            player_id=player_id,
            price=bid,
            dry_run=dry_run,
        )


# ------------------------------------------------------------
# main()
# ------------------------------------------------------------

def main():
    setup_logging()

    email, password, league_id, dry_run = get_env_credentials()
    logging.info("Starte Kickbase-Bot (DRY_RUN=%s)...", dry_run)

    with requests.Session() as session:
        # Login
        login_v4(session, email, password)

        # League-Me
        league_me = get_league_me_v4(session, league_id)
        budget_raw = league_me.get("b", 0)
        try:
            budget = int(budget_raw)
        except (TypeError, ValueError):
            budget = 0

        logging.info("Budget (aus JSON 'b'): %s", budget)

        # Markt holen
        market = get_market_v4(session, league_id)

        # Auto-Bieten
        select_and_bid(session, league_id, market, budget, dry_run)

    logging.info("Bot-Durchlauf fertig.")


if __name__ == "__main__":
    main()
