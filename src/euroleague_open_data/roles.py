"""Coach rotation profiles and role availability.

This is the part that answers the hard fantasy question: a player joins a new club, or
arrives from outside the competition, and there is no history to average.

The honest position is that no model can project such a player from this data alone --
there are literally zero rows for him. What CAN be computed is the *role* he is walking
into: how many minutes and how much shot volume the club has vacated at his position, and
how the coach distributes minutes. Combine that with your own read of the player's level
and you have a defensible estimate. The data supplies the container; the human supplies
the contents.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)


# A coach's rotation habits travel with him between clubs, which is why this is keyed on
# the coach rather than on the team.
COACH_ROTATION_SQL = """
CREATE OR REPLACE TABLE coach_rotation_profile AS
WITH game_coach AS (
    SELECT c.season_code, c.game_code, c.team_code, c.coach_code, c.coach_name
    FROM coaches c
    WHERE c.coach_code IS NOT NULL
),
lines AS (
    SELECT gc.season_code, gc.coach_code, gc.coach_name, gc.team_code,
           gc.game_code, b.person_code,
           b.seconds_played / 60.0 AS minutes
    FROM game_coach gc
    JOIN boxscores_player b
      ON b.season_code = gc.season_code
     AND b.game_code = gc.game_code
     AND b.team_code = gc.team_code
),
-- Total minutes actually distributed in each team-game, so shares can be computed
-- without nesting a window function inside an aggregate.
game_totals AS (
    SELECT season_code, team_code, game_code, sum(minutes) AS total_minutes
    FROM lines
    GROUP BY 1, 2, 3
),
shares AS (
    SELECT l.*, l.minutes / nullif(t.total_minutes, 0) AS minute_share
    FROM lines l
    JOIN game_totals t
      ON t.season_code = l.season_code
     AND t.team_code = l.team_code
     AND t.game_code = l.game_code
),
per_game AS (
    SELECT season_code, coach_code, coach_name, team_code, game_code,
           count(*) FILTER (WHERE minutes > 0)   AS players_used,
           count(*) FILTER (WHERE minutes >= 15) AS players_15plus,
           count(*) FILTER (WHERE minutes >= 20) AS players_20plus,
           max(minutes)                          AS top_minutes,
           -- Herfindahl index of minute shares: 1.0 would be one player on court for
           -- every minute, low values mean minutes are spread thin. This is the single
           -- number separating a short-rotation coach from a deep-rotation one.
           sum(pow(minute_share, 2))             AS minute_concentration
    FROM shares
    GROUP BY season_code, coach_code, coach_name, team_code, game_code
),
aggregated AS (
    SELECT
        season_code,
        coach_code,
        any_value(coach_name)               AS coach_name,
        any_value(team_code)                AS team_code,
        count(*)                            AS games_coached,
        round(avg(players_used), 2)         AS avg_players_used,
        round(avg(players_15plus), 2)       AS avg_players_15plus,
        round(avg(players_20plus), 2)       AS avg_players_20plus,
        round(avg(top_minutes), 1)          AS avg_top_minutes,
        round(avg(minute_concentration), 4) AS minute_concentration
    FROM per_game
    GROUP BY season_code, coach_code
)
SELECT
    a.*,
    -- Rotation depth is labelled RELATIVE TO THE COMPETITION, not against an absolute
    -- basketball norm. Measured on E2025 the whole league sits between 6.6 and 8.0
    -- players averaging 15+ minutes, so fixed thresholds would put everyone in one
    -- bucket and say nothing. Terciles say something.
    --
    -- Coaches with fewer than 10 games are not ranked: a two-game sample is noise, and
    -- labelling it would be worse than admitting ignorance.
    CASE
        WHEN a.games_coached < 10 THEN 'insufficient_data'
        ELSE CASE ntile(3) OVER (
                 PARTITION BY a.season_code
                 ORDER BY CASE WHEN a.games_coached >= 10 THEN a.avg_players_15plus END DESC
             )
             WHEN 1 THEN 'deep'
             WHEN 2 THEN 'balanced'
             ELSE 'short'
        END
    END AS rotation_style
FROM aggregated a
"""


# How reliably each player got minutes under his coach. In a draft league you keep the
# pick, so a player whose minutes swing wildly is a worse asset than his average suggests.
PLAYER_ROLE_SQL = """
CREATE OR REPLACE TABLE player_role_stability AS
WITH lines AS (
    SELECT b.season_code, b.person_code, b.team_code,
           b.seconds_played / 60.0 AS minutes,
           g.utc_date
    FROM boxscores_player b
    JOIN games g ON g.season_code = b.season_code AND g.game_code = b.game_code
    WHERE g.played
),
team_games AS (
    SELECT season_code, team_code, count(DISTINCT utc_date) AS team_games
    FROM lines GROUP BY 1, 2
)
SELECT
    l.season_code,
    l.person_code,
    p.name                                   AS player_name,
    l.team_code,
    p.position_name,
    tg.team_games,
    count(*) FILTER (WHERE l.minutes > 0)    AS games_played,
    round(count(*) FILTER (WHERE l.minutes > 0) * 1.0 / nullif(tg.team_games, 0), 3)
                                             AS availability,
    round(avg(l.minutes) FILTER (WHERE l.minutes > 0), 1) AS minutes_per_game,
    round(stddev_samp(l.minutes) FILTER (WHERE l.minutes > 0), 2) AS minutes_stddev,
    round(min(l.minutes) FILTER (WHERE l.minutes > 0), 1) AS minutes_worst,
    round(max(l.minutes), 1)                 AS minutes_best,
    -- Share of the team's total available court time (5 players x 40 minutes).
    round(sum(l.minutes) / nullif(tg.team_games * 200.0, 0), 4) AS team_minute_share
FROM lines l
JOIN team_games tg ON tg.season_code = l.season_code AND tg.team_code = l.team_code
LEFT JOIN players p ON p.person_code = l.person_code
GROUP BY l.season_code, l.person_code, p.name, l.team_code, p.position_name, tg.team_games
"""


# What a club has vacated going INTO a season.
#
# The obvious implementation -- filter the roster on `is_active = false` -- does not work,
# and it fails silently, which is worse. Measured on E2025 after the season ended, every
# player was still flagged active: upstream does not retire the flag when a contract
# lapses. A club that lost its starting centre looked identical to one that kept him.
#
# So departure is defined by comparison instead: a player who produced for club T in the
# previous season and does not appear on T's roster in this one has left, and his minutes
# are the opportunity. This needs TWO seasons loaded. With one season the table is
# correctly empty rather than full of misleading zeroes.
VACATED_ROLE_SQL = """
CREATE OR REPLACE TABLE vacated_role AS
WITH season_order AS (
    SELECT season_code, year,
           lag(season_code) OVER (PARTITION BY competition_code ORDER BY year) AS prev_season
    FROM seasons
),
prior_production AS (
    SELECT r.season_code, r.team_code, r.person_code,
           coalesce(r.position_name, 'Unknown') AS position_name,
           r.minutes_per_game,
           r.games_played,
           coalesce(f.modern_per_game, 0) AS fantasy_per_game
    FROM player_role_stability r
    LEFT JOIN fantasy_player_season f
      ON f.season_code = r.season_code AND f.person_code = r.person_code
    WHERE r.games_played > 0
),
-- Who is on the club's books in the later season, from any source we have.
current_roster AS (
    SELECT DISTINCT season_code, team_code, person_code FROM player_team_spells
    UNION
    SELECT DISTINCT season_code, team_code, person_code FROM boxscores_player
),
-- Only seasons whose rosters were actually loaded can be compared against. Without this
-- guard, the season after the last loaded one has an empty roster, so every player in
-- the warehouse reads as having "departed" -- an absence of data reported as a fact
-- about the world. Measured: with only E2025 loaded it claimed 340 departures from
-- E2026.
seasons_with_roster AS (
    SELECT DISTINCT season_code FROM current_roster
),
-- ...and only clubs that are actually IN the later season. A club that left the
-- competition looks like it vacated its entire roster, which is true and useless:
-- nobody is joining it. Measured E2024 -> E2025, ALBA Berlin left and contributed
-- 340 phantom vacated minutes across three positions.
teams_in_season AS (
    SELECT DISTINCT season_code, team_code FROM current_roster
),
departures AS (
    SELECT
        so.season_code                    AS season_code,
        p.team_code,
        p.position_name,
        p.person_code,
        p.minutes_per_game,
        p.fantasy_per_game,
        p.games_played
    FROM season_order so
    JOIN seasons_with_roster swr ON swr.season_code = so.season_code
    JOIN prior_production p ON p.season_code = so.prev_season
    JOIN teams_in_season tis
      ON tis.season_code = so.season_code AND tis.team_code = p.team_code
    LEFT JOIN current_roster c
      ON c.season_code = so.season_code
     AND c.team_code = p.team_code
     AND c.person_code = p.person_code
    WHERE so.prev_season IS NOT NULL
      AND c.person_code IS NULL
)
SELECT
    season_code,
    team_code,
    position_name,
    count(*)                                  AS departed_players,
    round(sum(minutes_per_game), 1)           AS vacated_minutes_per_game,
    round(sum(fantasy_per_game), 1)           AS vacated_fantasy_per_game,
    round(avg(minutes_per_game), 1)           AS avg_departed_minutes,
    max(minutes_per_game)                     AS biggest_departure_minutes
FROM departures
GROUP BY season_code, team_code, position_name
"""


def build(db_path: Path) -> None:
    con = duckdb.connect(str(db_path))
    for name, sql in [
        ("coach_rotation_profile", COACH_ROTATION_SQL),
        ("player_role_stability", PLAYER_ROLE_SQL),
        ("vacated_role", VACATED_ROLE_SQL),
    ]:
        con.execute(sql)
        count = con.execute(f"SELECT count(*) FROM {name}").fetchone()
        log.info("%-26s %s rows", name, count[0] if count else "?")
    con.close()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Build coach and role tables.")
    parser.add_argument("--db", type=Path, default=Path("data/euroleague.duckdb"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    build(args.db)


if __name__ == "__main__":
    main()
