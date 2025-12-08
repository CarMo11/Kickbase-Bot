import os
import logging
from datetime import datetime, timezone
from typing import Optional

from kickbase_api.kickbase import Kickbase as KickbaseBase
from kickbase_api.exceptions import KickbaseLoginException, KickbaseException
from kickbase_api.models._transforms import parse_date
from kickbase_api.models.user import User
from kickbase_api.models.league_data import LeagueData
from kickbase_api.models.market import Market


# ============================================================
# KONFIG
# ============================================================

# False = Bot schickt wirklich Gebote an Kickbase
# True  = Bot loggt nur, was er bieten WÜRDE
DRY_RUN = False

# Nur Spieler betrachten, deren Auktion in diesem Zeitfenster endet (Sekunden)
MAX_EXPIRY_WINDOW_SECONDS = 2 * 60 * 60  # 2 Stunden

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
    - market über /v4/leagues/{leagueId}/market
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

    def market(self, league) -> Market:
        """
        v4-Variante vom Transfermarkt: GET /v4/leagues/{leagueId}/market
        Wir loggen das rohe JSON und parsen es dann mit dem Market-Modell.
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
            # Nur für Info:
            items = j.get("it", []) or []
            logging.info("Spieler auf dem Markt (JSON 'it'): %d", len(items))

            # Library-Modell benutzen (lief in den Logs bereits ohne Fehler)
            m = Market(j)
            players = getattr(m, "players", []) or []
            logging.info(
                "Market-Objekt: players_count=%d",
                len(players),
            )
            return m
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
        und loggt Status + Body, damit wir Fehleranalyse machen können.
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


def expiry_to_datetime(expiry_raw: int) -> datetime:
    """
    Kickbase liefert expiry über das Model (vermutlich aus 'exs').
    Wir behandeln expiry_raw als Unix-Timestamp (Sekunden oder Millisekunden).
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
# BID-LOGIK (einfache Steiger-Logik)
# ============================================================


def decide_bid_smart(player, me_budget: int) -> Optional[int]:
    """
    Einfache Auto-Bid-Variante:

    - bietet NUR auf Steiger (mvt / market_value_trend > 0)
    - berücksichtigt Restlaufzeit (MAX_EXPIRY_WINDOW_SECONDS)
    - beachtet Budget-Buffer und max. Overpay

    Aktuell: bietet ungefähr Marktwert + 1 (bzw. +TrendFlag),
    gecappt auf MAX_OVERPAY_PCT über Marktwert.
    """

    mv = getattr(player, "market_value", 0) or 0
    # Trend-Flag: 0 = fällt, 1/2/... = steigt
    trend_flag = getattr(player, "market_value_trend", None)
    if trend_flag is None:
        trend_flag = getattr(player, "mvt", 0) or 0

    if mv <= 0:
        return None

    # Nicht alles Geld rausballern
    if me_budget <= MIN_CASH_BUFFER:
        return None

    # Restlaufzeit des Angebots
    secs_left = seconds_until_expiry(player.expiry)
    if secs_left < 0:
        return None

    if secs_left > MAX_EXPIRY_WINDOW_SECONDS:
        return None

    # Nur Steiger
    if trend_flag <= 0:
        return None

    # Ein kleines bisschen über Marktwert, orientiert am TrendFlag
    increment = max(1, int(trend_flag))
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

    # Transfermarkt holen
    try:
        market = kb.market(league)
    except KickbaseException:
        logging.error("market fehlgeschlagen – Bot bricht ab.")
        return

    players = getattr(market, "players", []) or []
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

        bid = decide_bid_smart(p, budget)
        if bid is None:
            continue

        name = f"{getattr(p, 'first_name', '')} {getattr(p, 'last_name', '')}".strip()
        mv = getattr(p, "market_value", 0)
        trend_flag = getattr(p, "market_value_trend", None)
        if trend_flag is None:
            trend_flag = getattr(p, "mvt", 0) or 0
        mins_left = int(secs_left // 60)

        logging.info(
            "Kandidat: %s | MW=%s | TrendFlag=%s | Rest=%d min | Gebot=%s",
            name,
            mv,
            trend_flag,
            mins_left,
            bid,
        )

        # Player-ID aus Model (je nach Library 'id' oder 'i')
        player_id = getattr(p, "id", None) or getattr(p, "i", None)
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
