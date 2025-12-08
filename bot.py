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
        me = kb.league_me(league)
    except KickbaseException:
        logging.error("league_me fehlgeschlagen – Bot bricht ab.")
        return

    budget = getattr(me, "budget", 0) or 0
    logging.info("Budget: %s | Teamwert: %s", budget, getattr(me, "team_value", None))

    # Transfermarkt holen
    try:
        market = kb.market(league)
    except KickbaseException:
        logging.error("market fehlgeschlagen – Bot bricht ab.")
        return

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
