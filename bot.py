import os
import logging
from datetime import datetime, timezone
from typing import Optional

from kickbase_api.kickbase import Kickbase as KickbaseBase
from kickbase_api.exceptions import KickbaseLoginException, KickbaseException
from kickbase_api.models._transforms import parse_date
from kickbase_api.models.user import User
from kickbase_api.models.league_data import LeagueData

# ============================================================
# KONFIG
# ============================================================

# True = Bot loggt nur, was er bieten WÜRDE
# False = Bot schickt wirklich Gebote an Kickbase
DRY_RUN = True

# Nur Spieler betrachten, deren Auktion in diesem Zeitfenster endet (Sekunden)
MAX_EXPIRY_WINDOW_SECONDS = 2 * 60 * 60  # 2 Stunden

# Mindestens so viel Geld soll nach einem Gebot noch übrig bleiben
MIN_CASH_BUFFER = 500_000  # z.B. 500k

# Maximaler Aufschlag auf Marktwert in Prozent (z.B. 10%)
MAX_OVERPAY_PCT = 0.10

# minimale "tägliche" Steigerung relativ zum Marktwert, z.B. 3 %
# (für deinen Steiger-/ROI-Filter)
MIN_DAILY_ROI = 0.03  # 0.03 = 3 %


# ============================================================
# PATCH: Kickbase v4 Wrapper – NUR LOGIN
# ============================================================


class Kickbase(KickbaseBase):
    """
    Wrapper um die originale Kickbase-API-Library:

    - Login über /v4/user/login (weil Kickbase den alten Login geändert hat)
    - ALLE anderen Endpoints (league_me, market, make_offer, ...) kommen
      aus der Original-Library und benutzen deren Models.

    Hintergrund: Unser erster Versuch, league_me/market selbst zu parsen,
    hat das JSON nicht korrekt in die Models gemappt → Budget=0, players=[].
    """

    def login(self, username: str, password: str):
        """
        Führt einen Login gegen /v4/user/login aus.

        Erfolgreich:
        - setzt self.token, self.token_expire, self.user
        - gibt (user, leagues) zurück wie die Original-Library

        Fehler:
        - wirft KickbaseLoginException bei 401
        - wirft KickbaseException bei allen anderen Fehlern
        """
        logging.info("Versuche Kickbase Login über /v4/user/login ...")

        data = {
            "em": username,
            "loy": False,
            "pass": password,
            "rep": {},
        }

        resp = self._do_post("/v4/user/login", data, False)
        status = resp.status_code

        try:
            j = resp.json()
        except Exception:
            body = resp.text
            logging.error(
                "Login-Antwort ist kein gültiges JSON. Status=%s, body[0:300]=%s",
                status,
                body[:300],
            )
            raise KickbaseException()

        logging.info("Login-HTTP-Status: %s", status)

        if status == 200:
            try:
                self.token = j["tkn"]
                self.token_expire = parse_date(j["tknex"])
                self.user = User(j["u"])
                leagues = [LeagueData(d) for d in j.get("srvl", [])]
            except KeyError as e:
                logging.error(
                    "Login-JSON-Struktur unerwartet, Key fehlt: %s, body=%s",
                    e,
                    j,
                )
                raise KickbaseException()

            logging.info(
                "Login erfolgreich. Eingeloggt als: %s",
                getattr(self.user, "name", None),
            )
            return self.user, leagues

        elif status == 401:
            logging.error(
                "Kickbase Login 401 Unauthorized – "
                "E-Mail/Passwort falsch oder Account gesperrt."
            )
            raise KickbaseLoginException()

        else:
            logging.error(
                "Kickbase Login fehlgeschlagen. Status=%s, body=%s",
                status,
                j,
            )
            raise KickbaseException()


# ============================================================
# HELFER
# ============================================================


def get_env(name: str, required: bool = True, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Umgebungsvariable {name} ist nicht gesetzt.")
    return value


def expiry_to_datetime(expiry_raw: int) -> datetime:
    """
    Kickbase liefert expiry als Unix-Timestamp.
    Je nach Implementierung sind das Sekunden oder Millisekunden.
    """
    if expiry_raw > 10**12:  # Millisekunden
        return datetime.fromtimestamp(expiry_raw / 1000, tz=timezone.utc)
    else:  # Sekunden
        return datetime.fromtimestamp(expiry_raw, tz=timezone.utc)


def seconds_until_expiry(expiry_raw: int) -> float:
    now = datetime.now(timezone.utc)
    exp = expiry_to_datetime(expiry_raw)
    return (exp - now).total_seconds()


# ============================================================
# BID-LOGIK (Steiger + ROI)
# ============================================================


def decide_bid_smart(player, me_budget: int) -> Optional[int]:
    """
    Smartere Auto-Bid-Variante:

    - bietet NUR auf Steiger (market_value_trend > 0)
    - bewertet die Steigerung relativ zum Marktwert (ROI)
    - berücksichtigt Restlaufzeit (z.B. nur <= 60 Min)
    - beachtet Budget-Buffer und max. Overpay

    Aktuell basiert das auf der letzten Steigerung (market_value_trend).
    Die "letzte 3 Updates"-Logik können wir später ergänzen, wenn wir
    Marktwert-Historie in einer DB speichern.
    """

    mv = getattr(player, "market_value", 0) or 0
    trend = getattr(player, "market_value_trend", 0) or 0

    if mv <= 0:
        return None

    # Nicht alles Geld rausballern
    if me_budget <= MIN_CASH_BUFFER:
        return None

    # Restlaufzeit des Angebots
    secs_left = seconds_until_expiry(player.expiry)
    if secs_left < 0:
        return None

    # engeres Zeitfenster: z.B. 60 Minuten statt 2 Stunden
    if secs_left > 60 * 60:
        return None

    # Nur Steiger
    if trend <= 0:
        return None

    # ROI = Steigerung im Verhältnis zum Marktwert
    roi = trend / mv  # z.B. 0.05 = 5 %

    # Mindest-ROI (billige, stark steigende Spieler bevorzugen)
    if roi < MIN_DAILY_ROI:
        return None

    # Du bist bereit, ungefähr eine weitere Steigerung vorzuzahlen
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


# ============================================================
# HAUPTLAUF
# ============================================================


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
    try:
        user, leagues = kb.login(email, password)
    except KickbaseLoginException:
        logging.error(
            "Login fehlgeschlagen: falsche Zugangsdaten oder Konto nicht für API-Login geeignet."
        )
        return
    except KickbaseException:
        logging.error("Login fehlgeschlagen: allgemeiner API-Fehler.")
        return

    logging.info("Eingeloggt als %s", getattr(user, "name", user))

    if not leagues:
        logging.error("Keine Liga gefunden – Bot bricht ab.")
        return

    # Alle Ligen einmal loggen, damit du die IDs siehst
    for l in leagues:
        logging.info("Liga gefunden: %s (ID=%s)", l.name, l.id)

    # Liga wählen (entweder gewünschte ID aus ENV oder erste Liga)
    if league_id_pref:
        league = next(
            (l for l in leagues if str(l.id) == str(league_id_pref)),
            leagues[0],
        )
    else:
        league = leagues[0]

    logging.info("Nutze Liga: %s (ID=%s)", league.name, league.id)

    # Eigene Budget-/Teamdaten holen (Original-Library)
    try:
        me = kb.league_me(league)
    except KickbaseException:
        logging.error("league_me fehlgeschlagen – Bot bricht ab.")
        return

    # Fallback: manche Versionen nennen das Feld evtl. anders
    budget = (
        getattr(me, "budget", None)
        or getattr(me, "money", None)
        or 0
    )
    team_value = getattr(me, "team_value", None) or getattr(me, "teamworth", None)

    logging.info("Budget: %s | Teamwert: %s", budget, team_value)

    # Transfermarkt holen (Original-Library)
    try:
        market = kb.market(league)
    except KickbaseException:
        logging.error("market fehlgeschlagen – Bot bricht ab.")
        return

    if getattr(market, "closed", False):
        logging.info("Transfermarkt ist aktuell geschlossen – nichts zu tun.")
        return

    players = market.players or []
    logging.info("Spieler auf dem Markt: %d", len(players))

    if not players:
        logging.info("Keine Spieler auf dem Markt.")
        return

    # Spieler nach Ablaufzeit sortieren (bald ablaufende zuerst)
    players_sorted = sorted(players, key=lambda p: expiry_to_datetime(p.expiry))

    for p in players_sorted:
        secs_left = seconds_until_expiry(p.expiry)

        # Grober Filter: nur Spieler im globalen Fenster
        if secs_left < 0 or secs_left > MAX_EXPIRY_WINDOW_SECONDS:
            continue

        # Smarte Bietlogik (Steiger, ROI, Buffer etc.)
        bid = decide_bid_smart(p, budget)
        if bid is None:
            continue

        name = f"{getattr(p, 'first_name', '')} {getattr(p, 'last_name', '')}".strip()
        mv = getattr(p, "market_value", 0)
        trend = getattr(p, "market_value_trend", 0) or 0
        mins_left = int(secs_left // 60)

        logging.info(
            "Kandidat: %s | MW=%s | Trend=%s | Rest=%d min | Gebot=%s",
            name,
            mv,
            trend,
            mins_left,
            bid,
        )

        if DRY_RUN:
            logging.info("DRY_RUN aktiv – Gebot wird NICHT gesendet.")
        else:
            logging.info("Sende Gebot %s für %s ...", bid, name)
            kb.make_offer(bid, p, league)
            # lokales Budget anpassen, damit wir im gleichen Run nicht zu viel verballern
            budget -= bid

    logging.info("Bot-Durchlauf fertig.")


if __name__ == "__main__":
    run_bot_once()
