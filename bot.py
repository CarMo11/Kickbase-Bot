import os
import logging
import math
from datetime import datetime, timezone
import requests
from dotenv import load_dotenv

# -----------------------------------------------------------------------------
# Konfiguration
# -----------------------------------------------------------------------------
BASE_URL_V4 = "https://api.kickbase.com/v4"
BASE_URL_V5 = "https://api.kickbase.com/v5"
BASE_URL = "https://api.kickbase.com"

# DRY_RUN=True => es werden KEINE echten Gebote gesendet, nur geloggt
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# Mindest-Restlaufzeit in Minuten, damit der Bot noch bietet
MIN_MINUTES_LEFT = 120  # z.B. mindestens 2 Stunden

# Nur Spieler mit diesen Trend-Flags (mvt) werden berücksichtigt
# 1 = steigt, 2 = stark steigt (ungefähre Bedeutung)
ALLOWED_TREND_FLAGS = {1, 2}

# Gebotsaufschlag (z.B. +1 auf Marktwert)
BID_INCREMENT = 1

# -----------------------------------------------------------------------------
# Logging konfigurieren
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# -----------------------------------------------------------------------------
# Umgebungsvariablen laden
# -----------------------------------------------------------------------------
load_dotenv()

EMAIL = os.getenv("KICKBASE_EMAIL")
PASSWORD = os.getenv("KICKBASE_PASSWORD")
LEAGUE_ID = os.getenv("KICKBASE_LEAGUE_ID")

if not EMAIL or not PASSWORD or not LEAGUE_ID:
    logging.error("KICKBASE_EMAIL, KICKBASE_PASSWORD oder KICKBASE_LEAGUE_ID fehlt!")
    raise SystemExit(1)


# -----------------------------------------------------------------------------
# HTTP Session
# -----------------------------------------------------------------------------
def create_session() -> requests.Session:
    session = requests.Session()
    # Ein halbwegs normaler User-Agent, nichts Verdächtiges
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Kickbase-Bot; +https://github.com/CarMo11/Kickbase-Bot)",
            "Accept": "application/json",
        }
    )
    return session


# -----------------------------------------------------------------------------
# Login
# -----------------------------------------------------------------------------
def login_v4(session: requests.Session, email: str, password: str) -> dict:
    """
    Führt Login gegen die Kickbase-API durch.

    WICHTIG:
    - Endpoint ohne /v4: /user/login
      ( /v4/user/login führt offenbar zu 401 AccessDenied )
    """
    url = f"{BASE_URL}/user/login"
    payload = {"email": email, "password": password}

    logging.info("Versuche Kickbase Login über %s ...", url)

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    r = session.post(url, json=payload, headers=headers, timeout=10)
    logging.info("Login-HTTP-Status: %s", r.status_code)

    if r.status_code != 200:
        # Zum Debuggen alles loggen
        logging.error("Login fehlgeschlagen: Status %s, Body=%s", r.status_code, r.text)
        raise RuntimeError(f"Login fehlgeschlagen (Status {r.status_code}): {r.text}")

    data = r.json()

    # Token aus Response holen (Name je nach API-Version)
    token = data.get("t") or data.get("token")
    if token:
        # Kickbase nutzt in der Regel kein "Bearer " Prefix sondern direkt den Token
        session.headers["Authorization"] = token
        logging.info("Authorization-Token gesetzt.")
    else:
        logging.warning("Kein Token im Login-Response gefunden. Folge-Requests könnten 401 liefern.")

    # Versuchen, den Usernamen zu loggen
    username = (
        data.get("n")
        or data.get("un")
        or data.get("user", {}).get("n")
        or "Unbekannt"
    )
    logging.info("Login erfolgreich. Eingeloggt als: %s", username)
    logging.info("Eingeloggt als %s", username)

    return data


# -----------------------------------------------------------------------------
# Liga-Daten
# -----------------------------------------------------------------------------
def get_league_me(session: requests.Session, league_id: str) -> dict:
    """
    Ruft /v4/team/league/me für die angegebene Liga auf.
    """
    url = f"{BASE_URL_V4}/team/league/me"
    params = {"leagueId": league_id}
    logging.info("Hole league_me JSON für Liga %s ...", league_id)

    r = session.get(url, params=params, timeout=10)
    logging.info("league_me Status: %s", r.status_code)

    if r.status_code != 200:
        logging.error("league_me fehlgeschlagen: Status=%s, Body=%s", r.status_code, r.text)
        raise RuntimeError(f"league_me failed with status {r.status_code}: {r.text}")

    data = r.json()
    logging.info("league_me raw JSON: %s", data)

    return data


def extract_budget(league_me_json: dict) -> int:
    """
    Nimmt das league_me JSON und zieht das Budget heraus.
    Oft ist das Feld 'b' (Budget) auf Root-Level.
    """
    budget = league_me_json.get("b")
    if budget is None:
        logging.warning("Konnte Budget im league_me JSON nicht finden. Fallback auf 0.")
        return 0

    try:
        budget_int = int(budget)
    except (TypeError, ValueError):
        logging.warning("Budget '%s' konnte nicht in int umgewandelt werden. Fallback 0.", budget)
        return 0

    logging.info("Budget (aus JSON 'b'): %s", budget_int)
    return budget_int


# -----------------------------------------------------------------------------
# Markt-Daten
# -----------------------------------------------------------------------------
def get_market(session: requests.Session, league_id: str) -> dict:
    """
    Ruft den Markt für die Liga ab: /v5/market?leagueId=...
    """
    url = f"{BASE_URL_V5}/market"
    params = {"leagueId": league_id}
    logging.info("Hole market JSON für Liga %s ...", league_id)

    r = session.get(url, params=params, timeout=10)
    logging.info("market Status: %s", r.status_code)

    if r.status_code != 200:
        logging.error("market fehlgeschlagen: Status=%s, Body=%s", r.status_code, r.text)
        raise RuntimeError(f"market failed with status {r.status_code}: {r.text}")

    data = r.json()
    logging.info("market raw JSON: %s", data)

    return data


# -----------------------------------------------------------------------------
# Spieler-Auswahl & Gebote
# -----------------------------------------------------------------------------
def minutes_left_from_exs(exs: int) -> int:
    """
    exs = Restlaufzeit in Sekunden (Interpretation aus deinem Log),
    wir rechnen das in Minuten um.
    """
    try:
        return math.floor(int(exs) / 60)
    except (TypeError, ValueError):
        return 0


def select_candidates(market_json: dict, budget: int):
    """
    Wählt Kandidaten vom Markt aus:
    - Liste 'it' im JSON enthält die Items
    - filtert nach TrendFlag und Restlaufzeit
    - prüft Budget
    """
    items = market_json.get("it", [])
    logging.info("Spieler auf dem Markt (JSON 'it'): %s", len(items))

    candidates = []

    for it in items:
        player_id = it.get("i")
        first_name = it.get("fn", "")
        last_name = it.get("n", "")
        full_name = f"{first_name} {last_name}".strip()

        mv = int(it.get("mv", 0))  # Marktwert
        trend_flag = it.get("mvt", 0)  # Trendflag
        exs = it.get("exs", 0)  # Restzeit in Sekunden
        price = int(it.get("prc", mv))  # aktueller Preis, fallback auf MV

        minutes_left = minutes_left_from_exs(exs)

        if trend_flag not in ALLOWED_TREND_FLAGS:
            continue

        if minutes_left < MIN_MINUTES_LEFT:
            continue

        bid_price = price + BID_INCREMENT

        if bid_price > budget:
            continue

        info = {
            "player_id": player_id,
            "name": full_name,
            "mv": mv,
            "trend_flag": trend_flag,
            "minutes_left": minutes_left,
            "bid_price": bid_price,
        }
        logging.info(
            "Kandidat: %s | MW=%s | TrendFlag=%s | Rest=%s min | Gebot=%s",
            full_name,
            mv,
            trend_flag,
            minutes_left,
            bid_price,
        )
        candidates.append(info)

    return candidates


def make_offer(session: requests.Session, league_id: str, player_id: str, price: int):
    """
    Sendet ein Gebot an die (vermutlich) richtige Offer-Route.

    Achtung:
    - Falls hier weiterhin 404 kommt, muss der Endpoint ggf. neu gesucht werden.
    """
    # Aktueller Versuch: POST /v4/market/{playerId}?leagueId=...
    url = f"{BASE_URL_V4}/market/{player_id}"
    params = {"leagueId": league_id}
    payload = {"price": price}

    logging.info("make_offer_v4: url=%s, league_id=%s, player_id=%s, price=%s", url, league_id, player_id, price)

    if DRY_RUN:
        logging.info("[DRY_RUN] Würde Gebot senden: %s", payload)
        return

    r = session.post(url, params=params, json=payload, timeout=10)

    if r.status_code != 200:
        logging.error("make_offer_v4 fehlgeschlagen: Status=%s, body=%s", r.status_code, r.text)
        raise RuntimeError(f"make_offer_v4 failed with status {r.status_code}")
    else:
        logging.info("Gebot erfolgreich gesendet. Response: %s", r.text)


# -----------------------------------------------------------------------------
# main()
# -----------------------------------------------------------------------------
def main():
    logging.info("Starte Kickbase-Bot (DRY_RUN=%s)...", DRY_RUN)

    session = create_session()

    # 1) Login
    login_data = login_v4(session, EMAIL, PASSWORD)

    # 2) league_me holen und Budget bestimmen
    league_me_json = get_league_me(session, LEAGUE_ID)
    budget = extract_budget(league_me_json)
    logging.info("Budget: %s", budget)

    # 3) Markt holen
    market_json = get_market(session, LEAGUE_ID)

    # 4) Kandidaten bestimmen
    candidates = select_candidates(market_json, budget)

    if not candidates:
        logging.info("Keine passenden Kandidaten gefunden.")
        return

    # 5) Für alle Kandidaten Gebote abgeben
    for cand in candidates:
        player_id = cand["player_id"]
        name = cand["name"]
        bid_price = cand["bid_price"]

        logging.info(
            "Sende Gebot %s für %s (player_id=%s, Liga=%s)...",
            bid_price,
            name,
            player_id,
            LEAGUE_ID,
        )

        try:
            make_offer(session, LEAGUE_ID, player_id, bid_price)
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
