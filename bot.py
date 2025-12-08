import os
import logging
from typing import Optional

from kickbase_api.kickbase import Kickbase as KickbaseBase
from kickbase_api.exceptions import KickbaseLoginException, KickbaseException
from kickbase_api.models._transforms import parse_date
from kickbase_api.models.user import User
from kickbase_api.models.league_data import LeagueData


# ============================================================
# KONFIG
# ============================================================

# False = Bot schickt wirklich Gebote an Kickbase
# True  = Bot loggt nur, was er bieten WÜRDE
DRY_RUN = False

# Nur Spieler betrachten, deren Auktion in diesem Zeitfenster endet (Sekunden)
# 24h, damit deine aktuellen Markt-Spieler mit ~2–12h Restlaufzeit mitgenommen werden
MAX_EXPIRY_WINDOW_SECONDS = 24 * 60 * 60  # 24 Stunden

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
    - league_me über /v4/leagues/{leagueId}/me
    - market über /v4/leagues/{leagueId}/market (wir nutzen das rohe JSON!)
    - Gebote über /v4/leagues/{leagueId}/market/{playerId}/offer
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

    def league_me(self, league) -> dict:
        """
        v4-Variante von league_me: GET /v4/leagues/{leagueId}/me

        Gibt das rohe JSON als dict zurück. Budget steckt im Feld 'b'.
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
            budget_raw = j.get("b", 0)
            try:
                budget_val = int(budget_raw)
            except (TypeError, ValueError):
                budget_val = 0
            logging.info("Budget (aus JSON 'b'): %s", budget_val)
            return j
        else:
            logging.error(
                "league_me fehlgeschlagen. Status=%s, body=%s",
                status,
                j,
            )
            raise KickbaseException()

    def market(self, league) -> dict:
        """
        v4 Transfermarkt: GET /v4/leagues/{leagueId}/market

        WICHTIG:
        - Wir geben das **rohe JSON** zurück.
        - Spieler stehen im Feld 'it' (Liste von dicts).
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
            items = j.get("it", []) or []
            logging.info("Spieler auf dem Markt (JSON 'it'): %d", len(items))
            return j
        else:
            logging.error(
                "market fehlgeschlagen. Status=%s, body=%s",
                status,
                j,
            )
            raise KickbaseException()

    def make_offer_v4(self, league, player_id: str, price: int):
        """
        Versucht, ein Gebot über den v4-Endpoint zu platzieren:

        POST /v4/leagues/{leagueId}/market/{playerId}/offer
        Body: { "prc": <Gebot> }

        Gibt bei Erfolg das JSON der API zurück, wirft sonst KickbaseException
        und loggt Status + Body.
        """
        league_id = self._get_league_id(league)
        price = int(price)

        logging.info(
            "make_offer_v4: league_id=%s, player_id=%s, price=%s",
            league_id, player_id, price,
        )

        payload = {"prc": price}

        resp = self._do_post(
            f"/v4/leagues/{league_id}/market/{player_id}/offer",
            payload,
            True,  # mit Auth
        )
        status = resp.status_code
        text = resp.text

        try:
            j = resp.json()
        except Exception:
            j = None

        if status in (200, 201):
            logging.info(
                "Gebot erfolgreich gesendet! Status=%s, body=%s",
                status,
                j if j is not None else text[:300],
            )
            return j

        logging.error(
            "make_offer_v4 fehlgeschlagen: Status=%s, body=%s",
            status,
            j if j is not None else text[:300],
        )
        raise KickbaseException(f"make_offer_v4 failed with status {status}")


# ============================================================
# HELFER
# ============================================================


def get_env(name: str, required: bool = True, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Umgebungsvariable {name} ist nicht gesetzt.")
    return value


# ============================================================
# BID-LOGIK (auf Basis des rohen JSON-Items)
# ============================================================


def decide_bid_smart(item: dict, me_budget: int) -> Optional[int]:
    """
    Einfache Auto-Bid-Variante auf Basis der rohen Markt-Items (JSON):

    Felder im Item:
      - mv  = Marktwert
      - mvt = Trend-Flag (0 = fällt/neutral, >0 = Steiger)
      - exs = Restlaufzeit in Sekunden

    Regeln:
      - bietet NUR auf Steiger (mvt > 0)
      - berücksichtigt Restlaufzeit (MAX_EXPIRY_WINDOW_SECONDS)
      - beachtet Budget-Buffer (MIN_CASH_BUFFER)
      - max. X % über Marktwert (MAX_OVERPAY_PCT)
    """

    mv = int(item.get("mv", 0) or 0)
    trend_flag = int(item.get("mvt", 0) or 0)
    secs_left = int(item.get("exs", 0) or 0)

    if mv <= 0:
        return None

    if me_budget <= MIN_CASH_BUFFER:
        return None

    # Restlaufzeit prüfen
    if secs_left < 0:
        return None
    if secs_left > MAX_EXPIRY_WINDOW_SECONDS:
        return None

    # Nur Steiger
    if trend_flag <= 0:
        return None

    # Ein kleines bisschen über Marktwert, orientiert am TrendFlag
    increment = max(1, trend_flag)
    bid = mv + increment

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

    # Eigene Budget-/Teamdaten holen
    try:
        me_json = kb.league_me(league)
    except KickbaseException:
        logging.error("league_me fehlgeschlagen – Bot bricht ab.")
        return

    budget_raw = me_json.get("b", 0)
    try:
        budget = int(budget_raw)
    except (TypeError, ValueError):
        budget = 0

    logging.info("Budget: %s", budget)

    # Transfermarkt holen (ROHES JSON)
    try:
        market_json = kb.market(league)
    except KickbaseException:
        logging.error("market fehlgeschlagen – Bot bricht ab.")
        return

    items = market_json.get("it", []) or []
    if not items:
        logging.info("Keine Spieler auf dem Markt (it-Liste leer).")
        return

    # Spieler nach Restlaufzeit sortieren (bald ablaufende zuerst)
    items_sorted = sorted(items, key=lambda x: int(x.get("exs", 10**9) or 10**9))

    for item in items_sorted:
        secs_left = int(item.get("exs", 0) or 0)
        mv = int(item.get("mv", 0) or 0)
        trend_flag = int(item.get("mvt", 0) or 0)
        mins_left = secs_left // 60

        # Bid-Entscheidung
        bid = decide_bid_smart(item, budget)
        if bid is None:
            continue

        fn = item.get("fn", "") or ""
        ln = item.get("n", "") or ""
        name = f"{fn} {ln}".strip() or f"Player {item.get('i')}"

        logging.info(
            "Kandidat: %s | MW=%s | TrendFlag=%s | Rest=%d min | Gebot=%s",
            name,
            mv,
            trend_flag,
            mins_left,
            bid,
        )

        player_id = str(item.get("i"))
        if not player_id:
            logging.error("Kein player_id für %s gefunden, überspringe.", name)
            continue

        if DRY_RUN:
            logging.info("DRY_RUN aktiv – Gebot wird NICHT gesendet.")
        else:
            logging.info(
                "Sende Gebot %s für %s (player_id=%s, Liga=%s)...",
                bid,
                name,
                player_id,
                league.id,
            )
            try:
                kb.make_offer_v4(league, player_id, bid)
                # Lokales Budget anpassen, damit im gleichen Run
                # nicht mehrfach dasselbe Geld verplant wird
                budget -= bid
            except KickbaseException as e:
                logging.error(
                    "make_offer fehlgeschlagen für %s (player_id=%s): %s",
                    name,
                    player_id,
                    e,
                )

    logging.info("Bot-Durchlauf fertig.")


if __name__ == "__main__":
    run_bot_once()
