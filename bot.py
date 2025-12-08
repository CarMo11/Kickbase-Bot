import os
import logging
from typing import Optional, Dict, Any, List

from kickbase_api.kickbase import Kickbase as KickbaseBase
from kickbase_api.exceptions import KickbaseLoginException, KickbaseException
from kickbase_api.models._transforms import parse_date
from kickbase_api.models.user import User
from kickbase_api.models.league_data import LeagueData


# ============================================================
# KONFIG
# ============================================================

# False = Bot schickt wirklich Gebote an Kickbase
# True  = nur loggen, was er tun WÜRDE
DRY_RUN = False

# Nur Spieler betrachten, deren Auktion in diesem Zeitfenster endet (Sekunden)
# (z.B. 24 Stunden)
MAX_EXPIRY_WINDOW_SECONDS = 24 * 60 * 60

# Mindestens so viel Geld soll nach einem Gebot noch übrig bleiben
MIN_CASH_BUFFER = 500_000  # z.B. 500k

# Maximaler Aufschlag auf Marktwert in Prozent (z.B. 10%)
MAX_OVERPAY_PCT = 0.10


# ============================================================
# PATCH: Kickbase v4 Wrapper
# ============================================================


class Kickbase(KickbaseBase):
    """
    Wrapper um die originale Kickbase-API-Library:

    - Login über /v4/user/login
    - league_me_json über /v4/leagues/{leagueId}/me
    - market_json über /v4/leagues/{leagueId}/market

    Gebote werden weiterhin über die vorhandene make_offer()-Methode
    der Library geschickt (alter Endpoint ohne /v4).
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

    # --------- v4 league_me als rohes JSON ---------

    def league_me_json(self, league) -> Dict[str, Any]:
        """
        v4-Variante von league_me: GET /v4/leagues/{leagueId}/me
        Gibt das rohe JSON zurück.
        """
        league_id = self._get_league_id(league)
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

        if status == 200:
            return j
        else:
            logging.error(
                "league_me fehlgeschlagen. Status=%s, body=%s",
                status,
                j,
            )
            raise KickbaseException()

    # --------- v4 market als rohes JSON ---------

    def market_json(self, league) -> Dict[str, Any]:
        """
        v4-Variante vom Transfermarkt: GET /v4/leagues/{leagueId}/market
        Gibt das rohe JSON zurück.
        """
        league_id = self._get_league_id(league)
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

        if status == 200:
            return j
        else:
            logging.error(
                "market fehlgeschlagen. Status=%s, body=%s",
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


def seconds_until_expiry_from_item(item: Dict[str, Any]) -> int:
    """
    Im v4-Market-JSON ist 'exs' = Sekunden bis Auktionsende.
    """
    return int(item.get("exs", 0) or 0)


# ============================================================
# BID-LOGIK (sehr simpel, auf v4 JSON angepasst)
# ============================================================


def decide_bid_smart(item: Dict[str, Any], me_budget: int) -> Optional[int]:
    """
    Entscheidung, ob und wie hoch geboten wird.

    Wir nutzen die v4-Market-Felder:
    - mv  = Marktwert
    - mvt = Trend-Flag (0=stabil, 1/2=steigend, 3/4=fallend etc.)
    - exs = Sekunden bis Auktionsende

    Heuristik:
    - nur auf Spieler mit steigendem Trend (mvt > 0)
    - nur innerhalb des globalen Zeitfensters (MAX_EXPIRY_WINDOW_SECONDS)
    - Overpay-Cap bei MAX_OVERPAY_PCT
    - Budgetpuffer MIN_CASH_BUFFER
    - Gebot ist nur minimal über Marktwert: mv + mvt
    """

    mv = int(item.get("mv", 0) or 0)
    trend_flag = int(item.get("mvt", 0) or 0)
    secs_left = seconds_until_expiry_from_item(item)

    if mv <= 0:
        return None

    # Nicht alles Geld rausballern
    if me_budget <= MIN_CASH_BUFFER:
        return None

    # Restlaufzeit (globaler Rahmen)
    if secs_left < 0 or secs_left > MAX_EXPIRY_WINDOW_SECONDS:
        return None

    # nur „Steiger“
    if trend_flag <= 0:
        return None

    # Basisgebot: minimal über Marktwert
    bid = mv + trend_flag

    # Hard-Cap: nicht mehr als MAX_OVERPAY_PCT über Marktwert
    max_allowed = int(mv * (1 + MAX_OVERPAY_PCT))
    if bid > max_allowed:
        bid = max_allowed

    # Budget-Check mit Puffer
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
    league_id_pref = os.environ.get("KICKBASE_LEAGUE_ID")  # deine Test-Liga

    logging.info("Starte Kickbase-Bot (DRY_RUN=%s)...", DRY_RUN)

    kb = Kickbase()

    # ------------------------------------
    # Login
    # ------------------------------------
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

    # Alle Ligen loggen
    for l in leagues:
        logging.info("Liga gefunden: %s (ID=%s)", l.name, l.id)

    # Liga wählen (ENV oder erste)
    if league_id_pref:
        league = next(
            (l for l in leagues if str(l.id) == str(league_id_pref)),
            leagues[0],
        )
    else:
        league = leagues[0]

    logging.info("Nutze Liga: %s (ID=%s)", league.name, league.id)

    # ------------------------------------
    # Eigene Budget-/Teamdaten (v4 JSON)
    # ------------------------------------
    try:
        me_json = kb.league_me_json(league)
    except KickbaseException:
        logging.error("league_me fehlgeschlagen – Bot bricht ab.")
        return

    budget_raw = me_json.get("b", 0)  # 'b' = Budget
    try:
        budget = int(budget_raw or 0)
    except (TypeError, ValueError):
        budget = 0

    logging.info("Budget (aus JSON 'b'): %s", budget)

    # ------------------------------------
    # Transfermarkt (v4 JSON)
    # ------------------------------------
    try:
        market_json = kb.market_json(league)
    except KickbaseException:
        logging.error("market fehlgeschlagen – Bot bricht ab.")
        return

    items: List[Dict[str, Any]] = market_json.get("it", []) or []
    logging.info("Spieler auf dem Markt (JSON 'it'): %d", len(items))

    if not items:
        logging.info("Keine Spieler auf dem Markt – nichts zu tun.")
        return

    # Spieler nach Restlaufzeit sortieren (kleinste exs zuerst)
    items_sorted = sorted(items, key=lambda it: seconds_until_expiry_from_item(it))

    # ------------------------------------
    # Biet-Loop
    # ------------------------------------
    for it in items_sorted:
        secs_left = seconds_until_expiry_from_item(it)

        # globaler Rahmen (zusätzliche Sicherung)
        if secs_left < 0 or secs_left > MAX_EXPIRY_WINDOW_SECONDS:
            continue

        bid = decide_bid_smart(it, budget)
        if bid is None:
            continue

        first_name = (it.get("fn") or "").strip()
        last_name = (it.get("n") or "").strip()
        name = (first_name + " " + last_name).strip() or it.get("n", "Unbekannt")

        mv = int(it.get("mv", 0) or 0)
        trend_flag = int(it.get("mvt", 0) or 0)
        mins_left = int(secs_left // 60)
        player_id = it.get("i")

        logging.info(
            "Kandidat: %s | MW=%s | TrendFlag=%s | Rest=%d min | Gebot=%s",
            name,
            mv,
            trend_flag,
            mins_left,
            bid,
        )

        if DRY_RUN:
            logging.info("DRY_RUN aktiv – Gebot wird NICHT gesendet.")
            continue

        if not player_id:
            logging.warning("Kein player_id im JSON – überspringe %s", name)
            continue

        # ------------------------------------
        # Hier wird WIRKLICH geboten
        # ------------------------------------
        try:
            logging.info(
                "Sende Gebot %s für %s (player_id=%s, Liga=%s)...",
                bid,
                name,
                player_id,
                league.id,
            )
            offer = kb.make_offer(bid, player_id, league)
            logging.info(
                "Gebot gesendet. Server-Antwort (Offer-Objekt): %s",
                getattr(offer, "__dict__", offer),
            )
            # Lokales Budget reduzieren, damit im selben Run nicht OVERSPENT wird
            budget -= bid
        except KickbaseException as e:
            logging.error(
                "make_offer fehlgeschlagen für %s (player_id=%s): %s",
                name,
                player_id,
                e,
            )
            # bei Fehler Budget nicht anpassen und weiter zum nächsten Spieler

    logging.info("Bot-Durchlauf fertig.")


if __name__ == "__main__":
    run_bot_once()
