"""Each player's current scoring level: a Kalman filter over the games he has played this season.

ESPN's season projection is a preseason number it almost never revisits. Justin Herbert's sat at
276 points on every one of 39 days in logs/jev_scores.csv while he scored ten a game, and the
waiver report, the drop choice and the trade analyzer all valued later weeks from it. A level
starts at that projection per game and moves toward what he actually scores, one game at a time.

The model is a local level: his true level drifts a little from game to game, and each game is a
noisy reading of it. Only two ratios decide the estimate, so there are two settings: how many
games the preseason projection counts for, and how far a level can drift per game. The drift
was fit on public 2025 results. The weights come from value_test.py on this league's own numbers
(weeks 1-9 of 2026), and that test is how to revisit them.

What it can't see is a role that changed yesterday. A backup's level takes weeks as a starter to
catch up. And at running back, where a level at the lighter weight had moved three or more
points from ESPN's number, ESPN's number was the closer of the two on average. So backs lean on
the projection, and roles.py handles the takeover.
"""
GAMES_PER_SEASON = 17   # turns a season projection into a per-game one
# How many games the preseason projection counts for. ESPN's number is a weak guide at quarterback,
# where jobs change hands (-10% average miss at 4 games), and a good one at back and receiver, where
# 16 did best (-4% and -3%) and 8 to 32 were within two hundredths of a point of it. A position
# that isn't here keeps ESPN's number: at tight end and defense no weight beat it, and at kicker
# the heaviest did by three hundredths of a point, which is noise.
PRIOR_GAMES = {"QB": 4, "RB": 16, "WR": 16}
DRIFT = 0.02            # how far his true level can move per game, as a share of one game's noise;
                        # it keeps every new game worth about 13% however late in the season it is
FILTERED = set(PRIOR_GAMES)


def level(prior, scores, prior_games, drift=DRIFT):
    """Points a game from here on, given his games so far, oldest first.

    prior_games is PRIOR_GAMES for his position. Variances are in units of one game's noise,
    which is why there is no noise setting.
    """
    estimate, variance = prior, 1.0 / prior_games
    for points in scores:
        variance += drift
        gain = variance / (variance + 1.0)
        estimate += gain * (points - estimate)
        variance *= 1.0 - gain
    return estimate


def finished_games(card, season, before_week):
    """ESPN's stat line for each game he has finished this season, in no particular order.

    Only weeks before the current one count, since this week's line can be a game still being
    played. A week with no stats is a game he missed, which says nothing about how he plays.
    """
    return [line for line in card.get("stats", [])
            if line.get("statSourceId") == 0 and line.get("seasonId") == season
            and line.get("statSplitTypeId") != 2 and line.get("stats")
            and line.get("appliedTotal") is not None
            and 0 < (line.get("scoringPeriodId") or 0) < before_week]


def games_played(card, season, before_week):
    """[(week, points)] in this league's scoring for the games he has finished, oldest first."""
    return sorted({line["scoringPeriodId"]: line["appliedTotal"]
                   for line in finished_games(card, season, before_week)}.items())


def attach(espn, players, week, final_week=18):
    """Give each quarterback, running back and receiver his level, in place.

    A player ESPN sends no card for is left as he is, and season_pace falls back to ESPN's number.
    Returns the cards, {player_id: card}, for anything else that reads a player's games (roles.py).
    """
    rated = [p for p in players if p["position"] in FILTERED]
    cards = espn.player_cards([p["id"] for p in rated], final_week)
    for player in rated:
        card = cards.get(player["id"])
        if card is None:
            continue
        games = games_played(card, espn.season, week)
        prior = (player["season_projection"] or 0.0) / GAMES_PER_SEASON
        player["games"] = len(games)
        player["level"] = level(prior, [points for _, points in games], PRIOR_GAMES[player["position"]])
    return cards


def season_pace(player):
    """Points over a full season at his level; ESPN's season projection where he has no level."""
    if player.get("level") is not None:
        return player["level"] * GAMES_PER_SEASON
    return player["season_projection"] or 0.0
