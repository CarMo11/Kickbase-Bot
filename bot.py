#!/usr/bin/env python3
"""
Einfacher Kickbase-Login-Bot nur zum Debuggen des 401-Problems in GitHub Actions.

Funktion:
- lädt ENV-Variablen (KICKBASE_EMAIL, KICKBASE_PASSWORD, KICKBASE_LEAGUE_ID)
- macht einen Login-Request gegen /v4/user/login
- loggt Statuscode + Response-Body
- loggt sicher, ob Email/Passwort überhaupt gesetzt sind (Längen, aber nicht Inhalt)

Wenn das hier in GitHub Actions weiterhin 401 {"err":"AccessDenied"} liefert,
dann ist das Problem sehr wahrscheinlich NICHT:
- falscher Endpoint
- JSON vs. Form-Daten
- Code im restlichen Bot

sondern eher:
- Kickbase akzeptiert das Login von dieser Umgebung/IP nicht
- Account hat Spezial-Login (z.B. Social Login, 2FA o.ä.)
- Kickbase hat Anti-Bot / Captcha / Rate Limiting o.ä.

Du startest das Script wie gehabt:
    python bot.py
"""

import json
import logging
import os
from typing import Tuple, Optional

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

BASE_URL = "https://api.kickbase.com"
LOGIN_PATH_V4 = "/v4/user/login"

# Wenn du hier einen realistischeren User-Agent einsetzen willst, kannst du das tun.
# Wichtig ist nur: in Actions und lokal dasselbe Verhalten.
DEFAULT_HEADERS = {
    "User-Agent": "Kickbase-Bot/0.1 (+github-actions)",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def load_env() -> Tuple[str, str, Optional[str]]:
    """
    Lädt KICKBASE_EMAIL, KICKBASE_PASSWORD, KICKBASE_LEAGUE_ID aus Umgebung
    (lokal zusätzlich aus .env via python-dotenv).
    """
    # .env nur lokal relevant – in GitHub Actions kommen die Werte aus secrets
    load_dotenv()

    email = os.getenv("KICKBASE_EMAIL")
    password = os.getenv("KICKBASE_PASSWORD")
    league_id = os.getenv("KICKBASE_LEAGUE_ID")

    # Niemals die Klarwerte loggen – nur Längen etc.
    logger.info(
        "ENV-Check: email_set=%s, password_set=%s, league_id_set=%s",
        bool(email),
        bool(password),
        bool(league_id),
    )

    if email:
        logger.info("KICKBASE_EMAIL length: %d", len(email))
    else:
        logger.error("KICKBASE_EMAIL ist NICHT gesetzt!")

    if password:
        logger.info("KICKBASE_PASSWORD length: %d", len(password))
    else:
        logger.error("KICKBASE_PASSWORD ist NICHT gesetzt!")

    if not email or not password:
        raise SystemExit(
            "Fehlende ENV-Variablen: KICKBASE_EMAIL und/oder KICKBASE_PASSWORD "
            "sind nicht gesetzt."
        )

    return email, password, league_id


def login_v4(session: requests.Session, email: str, password: str) -> dict:
    """
    Führt den Login gegen /v4/user/login durch und loggt alles Wichtige.

    Bei Fehler -> RuntimeError mit vollem Response-Text.
    """
    url = BASE_URL + LOGIN_PATH_V4

    # Payload wie im offiziellen Login: JSON mit email/password
    payload = {
        "email": email,
        "password": password,
    }

    logger.info("Versuche Kickbase Login über %s ...", url)
    logger.info(
        "Login-Payload: email_length=%d, password_length=%d",
        len(email),
        len(password),
    )

    try:
        response = session.post(
            url,
            headers=DEFAULT_HEADERS,
            data=json.dumps(payload),
            timeout=20,
        )
    except Exception as exc:
        logger.exception("HTTP-Request zu %s ist fehlgeschlagen: %s", url, exc)
        raise

    logger.info("Login-HTTP-Status: %s", response.status_code)

    text_preview = response.text[:500] if response.text else ""
    logger.info("Login-Response-Body (erste 500 Zeichen): %s", text_preview)

    if response.status_code != 200:
        logger.error(
            "Login fehlgeschlagen: Status %s, Body=%s",
            response.status_code,
            response.text,
        )
        raise RuntimeError(
            f"Login fehlgeschlagen (Status {response.status_code}): {response.text}"
        )

    try:
        data = response.json()
    except json.JSONDecodeError:
        logger.error("Login-Response ist kein gültiges JSON!")
        raise RuntimeError(f"Unerwarteter Login-Response: {response.text!r}")

    # Username/Token o.ä. ausgeben, falls vorhanden
    username = data.get("un") or data.get("username") or "<unbekannt>"
    logger.info("Login erfolgreich. Eingeloggt als: %s", username)

    token = data.get("t") or data.get("token")
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
        logger.info("Auth-Token im Session-Header gesetzt.")
    else:
        logger.warning("Kein Token im Login-Response gefunden – Folge-Requests könnten scheitern.")

    return data


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("Starte Kickbase-Bot (Login-Debug-Variante, DRY_RUN nur Login)...")

    email, password, league_id = load_env()

    # League-ID ist für den Login egal, aber wir loggen sie, um zu sehen,
    # ob sie aus ENV kommt:
    logger.info("KICKBASE_LEAGUE_ID (nur debug): %s", league_id)

    with requests.Session() as session:
        # Optional: Basis-Header für alle Requests
        session.headers.update(DEFAULT_HEADERS)

        login_data = login_v4(session, email, password)

        # Zur Sicherheit einmal das (gekürzte) Login-JSON loggen:
        try:
            pretty = json.dumps(login_data, indent=2, ensure_ascii=False)
        except TypeError:
            pretty = str(login_data)
        logger.info("Login-JSON (gekürzt):\n%s", pretty[:1000])

    logger.info("Bot-Durchlauf (Login-Debug) fertig.")


if __name__ == "__main__":
    main()
