import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

from kickbase_api.kickbase import Kickbase as KickbaseBase
from kickbase_api.exceptions import KickbaseLoginException, KickbaseException
from kickbase_api.models._transforms import parse_date
from kickbase_api.models.user import User
from kickbase_api.models.league_data import LeagueData

# ============================================================
# KONFIG
# ============================================================

# True = Bot loggt nur, was er bieten WÜRDE
# False = Bot schickt wirklich Gebote an Kickbase (noch nicht aktiviert)
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
    - ALLE anderen Endpoints nutzen wir hier manuell über _do_get/_do_post.

    Die alten Models (LeagueMe, Market, ...) passen nicht mehr sauber auf
    die v4-JSONs und haben Budget=0 / players=[] geliefert.
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

    # ---- v4-JSON direkt holen ---------------------------------

    def get_league_me_json(self, league_id: str) -> Dict[str, Any]:
        """
        /v4/leagues/{leagueId}/me → Roh-JSON zurückgeben.
        """
        logging.info("Hole league_me JSON für Liga %s ...", league_id)
        resp = self._do_get(f"/v4/leagues/{league_id}/me", True)
        status = resp.status_code
        try:
            j = resp.json()
        except Exception:
            body = resp.text
            logging.error(
                "league_me-Antwort kein gültiges JSON. Status=%s, body[0:300]=%s",
                status,
                body[:300],
            )
            raise KickbaseException()

        logging.info("league_me Status: %s", status)
        logging.info("league_me raw JSON: %s", j)

        if status != 200:
            logging.error("league_me fehlgeschlagen. body=%s", j)
            raise KickbaseException()

        return j

    def get_market_json(self, league_id: str) -> Dict[str, Any]:
        """
        /v4/leagues/{leagueId}/market → Roh-JSON zurückgeben.
        """
        logging.info("Hole market JSON für Liga %s ...", league_id)
        resp = self._do_get(f"/v4/leagues/{league_id}/market", True)
        status = resp.status_code
        try:
            j = resp.json()
        except Exception:
            body = resp.text
            logging.error(
                "market-Antwort kein gültiges JSON. Status=%s, body[0:300]=%s",
                status,
                body[:300],
            )
            raise KickbaseException()

        logging.info("market Status: %s", status)
        logging.info("market raw JSON: %s", j)

        if status != 200:
            logging.error("market fehlgeschlagen. body=%s", j)
            raise KickbaseException()

        return j


# ============================================================
# HELFER
# ============================================================


def get_env(name: str, required: bool = True, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Umgebungsvariable {name} ist nicht gesetzt.")
    return value


def expiry_to_datetime_from_exs(exs: int) -> datetime:
    """
    exs = "seconds until expiry" aus dem market-JSON.
    Wir rechnen das auf eine absolute Zeit um, damit Sorting/Logging hübsch ist.
    """
    now = datetime.now(timezone.utc)
    return now + timedelta(seconds=exs)


def seconds_until_expiry_from_exs(exs: int) -> float:
    """
    exs kommt direkt von Kickbase als "Restsekunden bis Auktionsende".
    """
    return float(exs)


# ============================================================
# BID-LOGIK (Steiger + ROI) – arbeitet auf dicts aus dem JSON
# ============================================================


def decide_bid_smart(player: Dict[str, Any], me_budget: int) -> Optional[int]:
    """
    Smartere Auto-Bid-Variante, direkt auf dem JSON-Item:

    Erwartete Felder im player-Dict:
    - 'mv'  : Marktwert (int)
    - 'mvt' : Marktwert-Trend (int, positiv = Steiger)
    - 'exs' : Restsekunden bis Auktionsende (int)
    - 'fn'  : Vorname
    - 'n'   : Nachname
    """

    mv = int(player.get("mv") or 0)
    trend = int(player.get("mvt") or 0)
    secs_left = int(player.get("exs") or 0)

    if mv <= 0:
        return None

    # Nicht alles Geld rausballern
    if me_budget <= MIN_CASH_BUFFER:
        return None

    # Restlaufzeit des Angebots
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

    league_id_str = str(league.id)
    logging.info("Nutze Liga: %s (ID=%s)", league.name, league_id_str)

    # ========================================================
    # League-Me JSON holen und Budget extrahieren
    # ========================================================

    try:
        me_json = kb.get_league_me_json(league_id_str)
    except KickbaseException:
        logging.error("league_me (JSON) fehlgeschlagen – Bot bricht ab.")
        return

    # In deinen v4-JSON-Logs war 'b': <Budget>
    raw_budget = me_json.get("b", 0)
    try:
        budget = int(raw_budget)
    except (TypeError, ValueError):
        budget = 0

    logging.info("Budget (aus JSON 'b'): %s", budget)

    # ========================================================
    # Markt-JSON holen und Spieler extrahieren
    # ========================================================

    try:
        market_json = kb.get_market_json(league_id_str)
    except KickbaseException:
        logging.error("market (JSON) fehlgeschlagen – Bot bricht ab.")
        return

    # In den v4-JSONs steckt der Markt unter 'it': [ {...}, {...}, ... ]
    items: List[Dict[str, Any]] = market_json.get("it", []) or []
    logging.info("Spieler auf dem Markt (JSON 'it'): %d", len(items))

    if not items:
        logging.info("Keine Spieler auf dem Markt.")
        return

    # Spieler nach Ablaufzeit sortieren (bald ablaufende zuerst)
    def sort_key(p: Dict[str, Any]) -> datetime:
        exs = int(p.get("exs") or 0)
        return expiry_to_datetime_from_exs(exs)

    players_sorted = sorted(items, key=sort_key)

    for p in players_sorted:
        secs_left = seconds_until_expiry_from_exs(int(p.get("exs") or 0))

        # Grober Filter: nur Spieler im globalen Fenster
        if secs_left < 0 or secs_left > MAX_EXPIRY_WINDOW_SECONDS:
            continue

        # Smarte Bietlogik (Steiger, ROI, Buffer etc.)
        bid = decide_bid_smart(p, budget)
        if bid is None:
            continue

        name = f"{p.get('fn', '')} {p.get('n', '')}".strip()
        mv = int(p.get("mv") or 0)
        trend = int(p.get("mvt") or 0)
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
            logging.info(
                "EIGENTLICH würde ich jetzt ein Gebot senden, "
                "aber make_offer_v4 ist noch nicht eingebaut."
            )

    logging.info("Bot-Durchlauf fertig.")


if __name__ == "__main__":
    run_bot_once()
