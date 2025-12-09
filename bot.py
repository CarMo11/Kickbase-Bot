import os
import logging
from datetime import datetime, timezone
import requests

# -------------------------------------------------------
# Konfiguration
# -------------------------------------------------------

DRY_RUN = False  # auf True setzen, wenn du erstmal nur testen willst

BASE_URL = "https://api.kickbase.com"
LOGIN_URL_V4 = f"{BASE_URL}/v4/user/login"

# Wie weit in die Zukunft darf ein Spieler noch auf dem Markt sein (in Minuten),
# damit wir ein Gebot abgeben?
MAX_MINUTES_LEFT = 24 * 60  # 24 Stunden

# Minimaler Marktwert, damit der Spieler überhaupt interessant ist
MIN_MARKET_VALUE = 500_000

# Trend: 1 = steigt, 2 = stark steigt (laut deinen Logs)
ALLOWED_TREND_FLAGS = {1, 2}

# -------------------------------------------------------
# Logging Setup
# -------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# -------------------------------------------------------
# Hilfsfunktionen
# -------------------------------------------------------

def get_env_var(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Umgebungsvariable {name} ist nicht gesetzt!")
    return value


def parse_kickbase_datetime(dt_str: str) -> datetime:
    """
    Kickbase gibt Datumswerte im Format '2025-12-08T20:59:23Z' zurück.
    """
    return datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# -------------------------------------------------------
# Login
# -------------------------------------------------------

def login_v4(session: requests.Session, email: str, password: str) -> dict:
    """
    Login über den funktionierenden v4-Endpoint.
    Nutzt JSON-Body mit 'email' und 'password'.
    """
    logging.info("Versuche Kickbase Login über /v4/user/login ...")

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        # ein halbwegs normaler User-Agent, nichts Exotisches
        "User-Agent": "KickbaseBot/1.0 (Python requests)",
    }

    payload = {
        "email": email,
        "password": password,
    }

    r = session.post(LOGIN_URL_V4, headers=headers, json=payload)
    logging.info("Login-HTTP-Status: %s", r.status_code)

    if r.status_code != 200:
        # Body mitloggen, um zu sehen, was Kickbase sagt (AccessDenied etc.)
        logging.error("Login fehlgeschlagen: Status %s, Body=%s", r.status_code, r.text)
        raise RuntimeError(f"Login fehlgeschlagen (Status {r.status_code}): {r.text}")

    data = r.json()
    username = data.get("un") or data.get("username") or "<unbekannt>"
    logging.info("Login erfolgreich. Eingeloggt als: %s", username)

    # Falls Kickbase ein Token oder ähnliches liefert, hier ins Session-Header setzen
    token = data.get("tkn") or data.get("token")
    if token:
        session.headers.update({"X-Auth-Token": token})

    return data


# -------------------------------------------------------
# Liga & Markt
# -------------------------------------------------------

def get_league_budget(session: requests.Session, league_id: str) -> float:
    """
    Holt Budget und league_me-Daten.
    Wir nutzen den nicht-versionierten /leagues/{id}/me Endpoint,
    der in deinen Logs 200 zurückgegeben hat.
    """
    url = f"{BASE_URL}/leagues/{league_id}/me"
    logging.info("Hole league_me JSON für Liga %s ...", league_id)
    r = session.get(url)
    logging.info("league_me Status: %s", r.status_code)

    if r.status_code != 200:
        logging.error("league_me fehlgeschlagen: Status=%s, Body=%s", r.status_code, r.text)
        raise RuntimeError(f"league_me fehlgeschlagen (Status {r.status_code})")

    data = r.json()
    logging.info("league_me raw JSON: %s", data)

    budget = data.get("b")
    if budget is None:
        raise RuntimeError("Konnte Budget 'b' nicht aus league_me JSON lesen")
    logging.info("Budget (aus JSON 'b'): %s", int(budget))
    return float(budget)


def get_market(session: requests.Session, league_id: str) -> dict:
    """
    Holt Markt-Daten der Liga.
    In deinen Logs war das JSON-Feld 'it' die Liste der Spieler.
    """
    url = f"{BASE_URL}/leagues/{league_id}/market"
    logging.info("Hole market JSON für Liga %s ...", league_id)
    r = session.get(url)
    logging.info("market Status: %s", r.status_code)

    if r.status_code != 200:
        logging.error("market fehlgeschlagen: Status=%s, Body=%s", r.status_code, r.text)
        raise RuntimeError(f"market fehlgeschlagen (Status {r.status_code})")

    data = r.json()
    logging.info("market raw JSON: %s", data)

    items = data.get("it", [])
    logging.info("Spieler auf dem Markt (JSON 'it'): %d", len(items))

    return data


def select_candidates(market_json: dict) -> list[dict]:
    """
    Filtert interessante Spieler vom Markt anhand deiner Kriterien:
    - MW >= MIN_MARKET_VALUE
    - Trendflag in ALLOWED_TREND_FLAGS
    - Restzeit <= MAX_MINUTES_LEFT
    """
    items = market_json.get("it", [])
    mv_update_str = market_json.get("mvud")  # z.B. "2025-12-09T21:00:00Z" (nächste Marktwert-Update)
    now_utc = datetime.now(timezone.utc)

    candidates = []

    for it in items:
        player_id = it.get("i")
        first_name = it.get("fn", "")
        last_name = it.get("n", "")
        name = (first_name + " " + last_name).strip() or f"ID {player_id}"
        mv = it.get("mv", 0)
        trend_flag = it.get("mvt", 0)
        price = it.get("prc", mv)
        dt_str = it.get("dt")  # Angebotsende? "2025-12-08T20:59:23Z"
        prob = it.get("prob", 0)

        # Grundfilter: MW & Trend
        if mv < MIN_MARKET_VALUE:
            continue
        if trend_flag not in ALLOWED_TREND_FLAGS:
            continue

        # Restzeit berechnen, falls dt vorhanden ist
        minutes_left = None
        if dt_str:
            try:
                end_dt = parse_kickbase_datetime(dt_str)
                minutes_left = int((end_dt - now_utc).total_seconds() // 60)
            except Exception:
                minutes_left = None

        # Wenn wir eine Restzeit haben, filtere nach MAX_MINUTES_LEFT
        if minutes_left is not None and minutes_left > MAX_MINUTES_LEFT:
            continue

        # kleines Overbid: 1–3 € über Marktwert, um nicht direkt überboten zu werden
        bid_price = max(price, mv) + 3

        candidates.append(
            {
                "player_id": player_id,
                "name": name,
                "mv": mv,
                "trend_flag": trend_flag,
                "prob": prob,
                "minutes_left": minutes_left,
                "bid_price": bid_price,
            }
        )

    return candidates


# -------------------------------------------------------
# Gebot senden
# -------------------------------------------------------

def make_offer(session: requests.Session, league_id: str, player_id: str, price: int) -> None:
    """
    Sendet ein Gebot an Kickbase.
    ACHTUNG: Endpunkt basiert auf typischem Kickbase-Schema.
    Wenn Kickbase das anders erwartet, bekommen wir 404 und
    sehen das im Log.
    """
    # Kandidat für Endpoint (es gibt keine Doku hier im Chat, wir stützen uns auf dein altes Muster)
    url = f"{BASE_URL}/v4/user/league/{league_id}/market/{player_id}/offer"

    payload = {
        "prc": price,
    }

    logging.info(
        "make_offer_v4: league_id=%s, player_id=%s, price=%s",
        league_id,
        player_id,
        price,
    )

    r = session.post(url, json=payload)
    if r.status_code != 200:
        logging.error(
            "make_offer_v4 fehlgeschlagen: Status=%s, body=%s",
            r.status_code,
            r.text,
        )
        raise RuntimeError(f"make_offer_v4 failed with status {r.status_code}")
    else:
        logging.info("Gebot erfolgreich gesendet (Status %s)", r.status_code)


# -------------------------------------------------------
# Hauptlogik
# -------------------------------------------------------

def main():
    email = get_env_var("KICKBASE_EMAIL")
    password = get_env_var("KICKBASE_PASSWORD")
    league_id = get_env_var("KICKBASE_LEAGUE_ID")

    logging.info("Starte Kickbase-Bot (DRY_RUN=%s)...", DRY_RUN)

    with requests.Session() as session:
        # Login
        login_data = login_v4(session, email, password)

        # Optional: Falls du später wieder über login_data die Ligen auslesen willst:
        # logging.info("Login-Response: %s", login_data)

        # Budget holen
        budget = get_league_budget(session, league_id)
        logging.info("Budget: %s", int(budget))

        # Markt holen
        market_json = get_market(session, league_id)

        # Kandidaten auswählen
        candidates = select_candidates(market_json)
        if not candidates:
            logging.info("Keine passenden Kandidaten gefunden.")
            return

        logging.info("Gefundene Kandidaten: %d", len(candidates))

        for c in candidates:
            name = c["name"]
            mv = c["mv"]
            trend_flag = c["trend_flag"]
            minutes_left = c["minutes_left"]
            bid_price = int(c["bid_price"])
            player_id = c["player_id"]

            rest_text = (
                f"{minutes_left} min"
                if minutes_left is not None
                else "unbekannt"
            )

            logging.info(
                "Kandidat: %s | MW=%s | TrendFlag=%s | Rest=%s | Gebot=%s",
                name,
                mv,
                trend_flag,
                rest_text,
                bid_price,
            )

            if DRY_RUN:
                logging.info(
                    "[DRY_RUN] Würde Gebot %s für %s (player_id=%s, Liga=%s) senden...",
                    bid_price,
                    name,
                    player_id,
                    league_id,
                )
                continue

            try:
                logging.info(
                    "Sende Gebot %s für %s (player_id=%s, Liga=%s)...",
                    bid_price,
                    name,
                    player_id,
                    league_id,
                )
                make_offer(session, league_id, player_id, bid_price)
            except Exception as e:
                logging.error(
                    "make_offer fehlgeschlagen für %s (player_id=%s): %s",
                    name,
                    player_id,
                    e,
                )

        logging.info("Bot-Durchlauf fertig.")


if __name__ == "__main__":
    main()
