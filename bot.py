import os
import logging
from datetime import datetime, timezone

from kickbase_api.kickbase import Kickbase  # kommt aus dem Paket "kickbase-api"


# ============== KONFIG ==============

# True = Bot loggt nur, was er bieten WÜRDE
# False = Bot schickt wirklich Gebote an Kickbase
DRY_RUN = True

# Nur Spieler betrachten, deren Auktion in diesem Zeitfenster endet (Sekunden)
MAX_EXPIRY_WINDOW_SECONDS = 2 * 60 * 60  # 2 Stunden

# Mindestens so viel Geld soll nach einem Gebot noch übrig bleiben
MIN_CASH_BUFFER = 500_000  # z.B. 500k

# Maximaler Aufschlag auf Marktwert in Prozent (z.B. 10%)
MAX_OVERPAY_PCT = 0.10


# ============== HELFER ==============

def get_env(name: str, required: bool = True, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Umgebungsvariable {name} ist nicht gesetzt.")
    return value


def expiry_to_datetime(expiry_raw: int) -> datetime:
    """
    Kickbase liefert expiry als Unix-Timestamp.
    Je nach Implementierung sind das Sekunden oder Millisekunden.
    """
    if expiry_raw > 10**12:
        # Millisekunden
        return datetime.fromtimestamp(expiry_raw / 1000, tz=timezone.utc)
    else:
        # Sekunden
        return datetime.fromtimestamp(expiry_raw, tz=timezone.utc)


def seconds_until_expiry(expiry_raw: int) -> float:
    now = datetime.now(timezone.utc)
    exp = expiry_to_datetime(expiry_raw)
    return (exp - now).total_seconds()


# ============== BID-LOGIK (einfach) ==============

def decide_bid_simple(player, me_budget: int) -> int | None:
    """
    Allereinfachste Auto-Bid-Variante:

    - nutzt market_value (MW) und market_value_trend (letzte Steigerung)
    - wenn Trend > 0: MW + Trend (gedeckelt)
    - wenn Trend <= 0: nur MW (Fangnetz)
    - Budget- und Sicherheitsgrenzen werden beachtet
    """
    mv = player.market_value or 0
    trend = getattr(player, "market_value_trend", 0) or 0

    if mv <= 0:
        return None

    # Nicht alles Geld rausballern
    if me_budget <= MIN_CASH_BUFFER:
        return None

    # Restlaufzeit des Angebots
    secs_left = seconds_until_expiry(player.expiry)
    if secs_left < 0 or secs_left > MAX_EXPIRY_WINDOW_SECONDS:
        # Entweder abgelaufen oder noch zu weit in der Zukunft
        return None

    # Extrem simple Gebotslogik
    if trend <= 0:
        bid = mv
    else:
        # "Letzte Steigerung drauf"
        bid = mv + trend

    # Sicherheits-Cap: max. X % über Marktwert
    max_allowed = int(mv * (1 + MAX_OVERPAY_PCT))
    bid = min(bid, max_allowed)

    # Nie weniger als den aktuellen Marktwert bieten
    bid = max(bid, mv)

    # Budget-Check: nach dem Gebot soll noch MIN_CASH_BUFFER übrig sein
    if me_budget - bid < MIN_CASH_BUFFER:
        return None

    return int(bid)


# ============== HAUPTLAUF ==============

def run_bot_once():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    email = get_env("KICKBASE_EMAIL")
    password = get_env("KICKBASE_PASSWORD")
    league_id_pref = os.environ.get("KICKBASE_LEAGUE_ID")

    logging.info("Starte Kickbase-Bot (DRY_RUN=%s)...", DRY_RUN)

    kb = Kickbase()

    # Login
    user, leagues = kb.login(email, password)
    logging.info("Eingeloggt als %s", getattr(user, "name", user))

    if not leagues:
        logging.error("Keine Liga gefunden – Bot bricht ab.")
        return

    # Liga wählen (entweder gewünschte ID oder erste Liga)
    if league_id_pref:
        league = next((l for l in leagues if str(l.id) == str(league_id_pref)), leagues[0])
    else:
        league = leagues[0]

    logging.info("Nutze Liga: %s (ID=%s)", league.name, league.id)

    # Eigene Budget-/Teamdaten holen
    me = kb.league_me(league)
    budget = me.budget or 0
    logging.info("Budget: %s | Teamwert: %s", budget, me.team_value)

    # Transfermarkt holen
    market = kb.market(league)
    if getattr(market, "closed", False):
        logging.info("Transfermarkt ist aktuell geschlossen – nichts zu tun.")
        return

    players = market.players or []
    if not players:
        logging.info("Keine Spieler auf dem Markt.")
        return

    # Spieler nach Ablaufzeit sortieren (bald ablaufende zuerst)
    players_sorted = sorted(players, key=lambda p: expiry_to_datetime(p.expiry))

    for p in players_sorted:
        secs_left = seconds_until_expiry(p.expiry)
        if secs_left < 0 or secs_left > MAX_EXPIRY_WINDOW_SECONDS:
            continue

        bid = decide_bid_simple(p, budget)
        if bid is None:
            continue

        name = f"{p.first_name} {p.last_name}".strip()
        mv = p.market_value
        trend = getattr(p, "market_value_trend", 0) or 0
        mins_left = int(secs_left // 60)

        logging.info(
            "Kandidat: %s | MW=%s | Trend=%s | Rest=%d min | Gebot=%s",
            name, mv, trend, mins_left, bid
        )

        if DRY_RUN:
            logging.info("DRY_RUN aktiv – Gebot wird NICHT gesendet.")
        else:
            logging.info("Sende Gebot %s für %s ...", bid, name)
            kb.make_offer(bid, p, league)
            budget -= bid  # intern Budget reduzieren

    logging.info("Bot-Durchlauf fertig.")


if __name__ == "__main__":
    run_bot_once()
