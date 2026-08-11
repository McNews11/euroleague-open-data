"""Announced squads for a season that has not been played yet, and the moves they imply.

Everything else in this warehouse is built from games. A draft happens before any game
exists, so the one thing a drafter most needs -- who is on which club now -- cannot come
from box scores. It comes from the announced rosters.

Two upstream properties shape this module.

The season-wide `people` route reports `total: 0` for an unplayed season while the
per-club route returns full squads, so rosters are fetched club by club.

Club names carry sponsors and are rewritten every summer without anyone moving: Maccabi
Playtika became Maccabi Rapyd, Virtus Segafredo became Virtus, EA7 Emporio Armani became
Armani Olimpia. Comparing names turns a rebrand into a squad-wide transfer -- it inflated
the move count here from 61 to 85 before this was fixed. Codes are stable; names are
marketing. Every comparison in this file joins on `team_code`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import duckdb

if TYPE_CHECKING:  # pragma: no cover
    from .http import ThrottledClient

# The crawler's dependencies are deliberately absent from the deployed image -- httpx is
# needed to BUILD a warehouse, never to read one. The MCP server imports this module only
# for its SQL, so anything that reaches for the network is imported inside the function
# that needs it. Importing it at module scope crashed a deployment with
# ModuleNotFoundError: httpx, before the server had served a single request.

log = logging.getLogger(__name__)

PLAYER_TYPE = "J"  # 'J' = player; coaches and staff share the endpoint

# Minute pressure: what a club's announced squad is used to playing at a position, over
# what the club actually gave that position last season. Above 1 is normal -- squads always
# carry more nominal minutes than a game has -- so only the league-relative level means
# anything. Across the 57 club x position pairs for 2026-27 the median is 1.11 and the
# terciles fall at 0.89 and 1.25.
#
# Backtested before being trusted, by building the same ratio from E2024 rosters and
# checking it against what actually happened in E2025 (n=149):
#
#     pressure <= 0.89   mean change +1.23   declined 8/30  (27%)
#     0.89 - 1.25        mean change +1.41   declined 14/38 (37%)
#     pressure >= 1.25   mean change -1.50   declined 57/81 (70%)
#
# Correlation -0.28: real but weak. The gap between the outer terciles is ~2.8 classic
# points, which is the whole size of the effect and therefore the whole size of the
# penalty. It is applied as a flat step rather than a curve because one season pair does
# not support fitting one, and a smooth function would imply precision that is not there.
PRESSURE_HIGH = 1.25
PRESSURE_LOW = 0.89
PRESSURE_PENALTY = 2.8

# EuroCup production, converted to a EuroLeague equivalent.
#
# 40 players went U2024 -> E2025 with 10+ games in both. Median ratio 0.57, mean 0.58.
# That single number hides the only thing that decides it:
#
#     kept 80%+ of their minutes   n=19   median 0.84
#     lost minutes                 n=21   median 0.21
#
# The step between competitions costs almost nothing. Not playing costs everything.
# Whether pressure predicts which happens was checked too, and the direction holds --
# low-pressure arrivals landed at 0.57, high-pressure at 0.24 -- but with n=4 in the high
# group that is not enough to justify a two-factor model. So one conservative factor is
# applied here and the ordinary pressure penalty does its work on top, as it does for
# everyone else. The two may overlap slightly; these rows are the least certain on the
# board and are labelled as such rather than blended in silently.
EUROCUP_FACTOR = 0.57
EUROCUP_MIN_GAMES = 8

# Minutes each club actually gave each position last season, and what the announced squad
# would want if everyone kept their previous workload.
PRESSURE_SQL = """
WITH last_spell AS (
    SELECT person_code, team_code, position_name FROM (
        SELECT s.person_code, s.team_code, s.position_name,
               row_number() OVER (PARTITION BY s.person_code
                                  ORDER BY s.start_date DESC) AS rn
        FROM player_team_spells s WHERE s.season_code = ?
    ) WHERE rn = 1
),
-- Per-game minutes at a position = the club's total minutes there divided by the games
-- the CLUB actually played. Not max(games_played) of any one player, who may have missed
-- half the season, and not a constant: a EuroLeague season is 34-42 games depending on
-- playoff run, and EuroCup is shorter still.
club_games AS (
    SELECT team_code, count(DISTINCT game_code) AS played FROM (
        SELECT home_team_code AS team_code, game_code FROM games WHERE season_code = ?
        UNION ALL
        SELECT away_team_code AS team_code, game_code FROM games WHERE season_code = ?
    ) GROUP BY 1
),
pot AS (
    SELECT l.team_code, l.position_name AS position,
           sum(st.minutes) / nullif(max(g.played), 0) AS pot_minutes
    FROM last_spell l
    JOIN player_season_stats st
      ON st.person_code = l.person_code AND st.season_code = ?
    LEFT JOIN club_games g ON g.team_code = l.team_code
    GROUP BY 1, 2
),
claim AS (
    SELECT a.team_code, a.position,
           sum(coalesce(st.minutes / nullif(st.games_played, 0), 0)) AS claimed
    FROM announced_rosters a
    LEFT JOIN player_season_stats st
      ON st.person_code = a.person_code AND st.season_code = ?
    WHERE a.season_code = ?
    GROUP BY 1, 2
)
SELECT a.person_code, a.team_name AS next_team, a.position AS next_position,
       round(c.claimed / nullif(p.pot_minutes, 0), 2) AS minute_pressure
FROM announced_rosters a
LEFT JOIN claim c ON c.team_code = a.team_code AND c.position = a.position
LEFT JOIN pot   p ON p.team_code = a.team_code AND p.position = a.position
WHERE a.season_code = ? AND a.person_code <> ''
"""


# Players on a next-season roster whose only history is the second-tier competition.
# Without this they read as "new_to_competition" and vanish from the board entirely, even
# though a full season of them is sitting in the warehouse.
EUROCUP_FALLBACK_SQL = f"""
WITH candidate AS (
    SELECT a.person_code, a.player_name, a.position, a.team_name
    FROM announced_rosters a
    WHERE a.season_code = ? AND a.person_code <> ''
      AND a.person_code NOT IN (
          SELECT DISTINCT person_code FROM fantasy_points_game WHERE season_code = ?)
      AND a.person_code NOT IN (
          SELECT person_code FROM player_overrides
          WHERE status IN ('left_league', 'retired', 'unavailable'))
),
-- Best available evidence, in order. A real EuroLeague number from an older season beats
-- a converted one from the second tier: Zizic played the EuroLeague in 2024-25 and
-- averaged 8.85 there, which is worth more than his 22.19 EuroCup average scaled by a
-- league-wide factor. Ranking him on the estimate buried the fact.
graded AS (
    SELECT c.person_code, c.player_name, c.position, c.team_name,
           f.season_code, avg(f.fantasy_classic) AS raw, count(*) AS gp,
           CASE WHEN f.season_code LIKE 'E%' THEN 1 ELSE 2 END AS tier
    FROM candidate c
    JOIN fantasy_points_game f ON f.person_code = c.person_code
    GROUP BY 1, 2, 3, 4, 5
    HAVING count(*) >= {EUROCUP_MIN_GAMES}
),
best AS (
    SELECT *, row_number() OVER (
        PARTITION BY person_code ORDER BY tier, season_code DESC) AS rn
    FROM graded
)
SELECT person_code, player_name, position, team_name AS next_team,
       position AS next_position,
       round(raw * CASE WHEN tier = 1 THEN 1.0 ELSE {EUROCUP_FACTOR} END, 2) AS value_per_game,
       round(raw, 2) AS source_value,
       gp AS games_played,
       season_code AS source_season,
       CASE WHEN tier = 1 THEN season_code
            ELSE season_code || ' x ' || {EUROCUP_FACTOR} END AS value_source
FROM best WHERE rn = 1
"""


def pressure_params(previous_season: str, next_season: str) -> list[str]:
    """Bind values for PRESSURE_SQL, in order.

    Kept next to the query on purpose: the placeholders and their arguments have to change
    together, and counting `?` by eye at the call site is how a working board turned into
    "Values were not provided for prepared statement parameter 10".
    """
    return [
        previous_season,  # last_spell
        previous_season,  # club_games, home leg
        previous_season,  # club_games, away leg
        previous_season,  # pot
        previous_season,  # claim, player stats
        next_season,      # claim, announced roster
        next_season,      # outer select
    ]


def fetch_rosters(client: ThrottledClient, comp: str, season: str) -> list[dict[str, Any]]:
    """Every club's announced squad for `season`, one request per club."""
    from . import sources

    envelope = client.get_json(sources.clubs(comp, season))
    clubs = (envelope or {}).get("data") or []
    if not clubs:
        log.warning("no clubs listed for %s", season)
        return []

    rows: list[dict[str, Any]] = []
    for club in clubs:
        code, name = club.get("code"), club.get("name")
        people = client.get_json(sources.club_people(comp, season, code)) or []
        signed = 0
        for entry in people:
            if entry.get("type") != PLAYER_TYPE:
                continue
            person = entry.get("person") or {}
            person_code = (person.get("code") or "").strip()
            rows.append(
                {
                    "season_code": season,
                    "team_code": code,
                    "team_name": name,
                    # Players new to the competition have no code yet. Kept with an empty
                    # one so squad sizes stay truthful; joins to history simply miss them,
                    # which is the honest result rather than a dropped row.
                    "person_code": person_code,
                    "player_name": person.get("name"),
                    "position": entry.get("positionName"),
                    "dorsal": (entry.get("dorsal") or "").strip() or None,
                    "contract_until": (entry.get("endDate") or "")[:10] or None,
                    # Named for what it is. A code is not history: Valanciunas carries one
                    # from 2011-13 while this warehouse holds no row for him.
                    "has_person_code": bool(person_code),
                }
            )
            signed += 1
        log.info("%s %s: %d players", season, name, signed)
    return rows


def load(con: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]]) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS announced_rosters (
            season_code VARCHAR, team_code VARCHAR, team_name VARCHAR,
            person_code VARCHAR, player_name VARCHAR, position VARCHAR,
            dorsal VARCHAR, contract_until VARCHAR, has_person_code BOOLEAN
        )
        """
    )
    # CREATE TABLE IF NOT EXISTS does not alter an existing table, so a warehouse built
    # before the rename keeps the old column while the code refers to the new one. Silent
    # today because no query reads the flag; a trap the next time one does.
    columns = {r[1] for r in con.execute("PRAGMA table_info('announced_rosters')").fetchall()}
    if "known_to_warehouse" in columns and "has_person_code" not in columns:
        con.execute(
            "ALTER TABLE announced_rosters RENAME COLUMN known_to_warehouse TO has_person_code"
        )
        log.info("migrated announced_rosters.known_to_warehouse -> has_person_code")

    if not rows:
        return
    season = rows[0]["season_code"]
    con.execute("DELETE FROM announced_rosters WHERE season_code = ?", [season])
    con.executemany(
        "INSERT INTO announced_rosters VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            [r["season_code"], r["team_code"], r["team_name"], r["person_code"],
             r["player_name"], r["position"], r["dorsal"], r["contract_until"],
             r["has_person_code"]]
            for r in rows
        ],
    )
    log.info("announced_rosters: %d rows for %s", len(rows), season)


# Joined on team_code precisely so a sponsor rename is not read as a transfer.
TRANSFERS_SQL = """
WITH now AS (
    SELECT person_code, player_name, team_code, team_name, position
    FROM announced_rosters
    WHERE season_code = ? AND person_code <> ''
),
-- A player can hold several spells in one season: Saben Lee played for Olympiacos until
-- December and Anadolu Efes from January. "Which club did he leave" means the last one,
-- so spells are ranked by start date and only the final one is kept. Without this the
-- join fans out and the same player is reported as moving from two different clubs.
--
-- The teams join carries season_code as well, because teams holds one row per club per
-- season and joining on code alone duplicates every row again.
before AS (
    SELECT person_code, team_code, team_name FROM (
        SELECT s.person_code, s.team_code,
               coalesce(t.name, s.team_name) AS team_name,
               row_number() OVER (PARTITION BY s.person_code
                                  ORDER BY s.start_date DESC) AS rn
        FROM player_team_spells s
        LEFT JOIN teams t
               ON t.team_code = s.team_code AND t.season_code = s.season_code
        WHERE s.season_code = ?
    ) WHERE rn = 1
),
form AS (
    SELECT person_code,
           round(avg(fantasy_classic), 2) AS classic_per_game,
           count(*) AS games_played
    FROM fantasy_points_game
    WHERE season_code = ?
    GROUP BY 1
)
SELECT n.player_name, n.position,
       b.team_name AS from_team, n.team_name AS to_team,
       b.team_code AS from_team_code, n.team_code AS to_team_code,
       f.classic_per_game, f.games_played,
       CASE WHEN b.person_code IS NULL THEN 'new_to_competition'
            WHEN b.team_code <> n.team_code THEN 'moved'
            ELSE 'stayed' END AS status
FROM now n
LEFT JOIN before b USING (person_code)
LEFT JOIN form f ON f.person_code = n.person_code
"""

# What a club lost for the UPCOMING season, by position.
#
# The vacated_role table compares two seasons that were both played, so before a ball is
# thrown up it describes last summer, not this one -- reading it as "minutes free at
# Zalgiris now" is wrong by a whole transfer window. This compares the announced roster
# against the last played season instead.
DEPARTURES_SQL = """
WITH last_spell AS (
    SELECT person_code, team_code, position_name FROM (
        SELECT s.person_code, s.team_code, s.position_name,
               row_number() OVER (PARTITION BY s.person_code
                                  ORDER BY s.start_date DESC) AS rn
        FROM player_team_spells s WHERE s.season_code = ?
    ) WHERE rn = 1
),
form AS (
    SELECT person_code, avg(fantasy_classic) AS classic, count(*) AS games
    FROM fantasy_points_game WHERE season_code = ? GROUP BY 1
),
mins AS (
    SELECT person_code, minutes / nullif(games_played, 0) AS mpg, usage_pct
    FROM player_season_stats WHERE season_code = ?
)
SELECT l.position_name AS position, p.name AS player_name,
       round(m.mpg, 1) AS minutes_per_game, m.usage_pct,
       round(f.classic, 2) AS classic_per_game, f.games AS games_played
FROM last_spell l
JOIN players p USING (person_code)
LEFT JOIN form f USING (person_code)
LEFT JOIN mins m USING (person_code)
WHERE l.team_code = ?
  AND l.person_code NOT IN (
      SELECT person_code FROM announced_rosters
      WHERE season_code = ? AND team_code = ? AND person_code <> ''
  )
ORDER BY m.mpg DESC NULLS LAST
"""

# The announced squad, with each player's last-season workload where one exists.
#
# A null is the honest answer here, not a gap to fill. Valanciunas returns to Zalgiris
# carrying a person code from 2011-13 and zero rows in this warehouse; an NBA arrival has
# neither. Both must read as "no basis for a number", never as zero.
SQUAD_SQL = """
WITH prev AS (
    SELECT person_code, team_code FROM (
        SELECT person_code, team_code,
               row_number() OVER (PARTITION BY person_code
                                  ORDER BY start_date DESC) AS rn
        FROM player_team_spells WHERE season_code = ?
    ) WHERE rn = 1
),
form AS (
    SELECT person_code, avg(fantasy_classic) AS classic, count(*) AS games
    FROM fantasy_points_game WHERE season_code = ? GROUP BY 1
),
mins AS (
    SELECT person_code, minutes / nullif(games_played, 0) AS mpg, usage_pct
    FROM player_season_stats WHERE season_code = ?
)
SELECT a.player_name, a.position, a.dorsal,
       round(m.mpg, 1) AS minutes_per_game, m.usage_pct,
       round(f.classic, 2) AS classic_per_game, f.games AS games_played,
       CASE WHEN a.person_code = '' THEN 'new_to_competition'
            WHEN f.games IS NULL THEN 'no_history_in_warehouse'
            WHEN pr.team_code = a.team_code THEN 'returning'
            ELSE 'arrived' END AS status
FROM announced_rosters a
LEFT JOIN prev pr ON pr.person_code = a.person_code
LEFT JOIN form f ON f.person_code = a.person_code
LEFT JOIN mins m ON m.person_code = a.person_code
WHERE a.season_code = ? AND a.team_code = ?
ORDER BY m.mpg DESC NULLS LAST
"""

# Players from last season who appear on no announced roster. They may still sign, so this
# is "not currently listed", not "gone" -- the distinction matters in August.
UNSIGNED_SQL = """
WITH last_spell AS (
    SELECT person_code, team_name FROM (
        SELECT s.person_code, coalesce(t.name, s.team_name) AS team_name,
               row_number() OVER (PARTITION BY s.person_code
                                  ORDER BY s.start_date DESC) AS rn
        FROM player_team_spells s
        LEFT JOIN teams t
               ON t.team_code = s.team_code AND t.season_code = s.season_code
        WHERE s.season_code = ?
    ) WHERE rn = 1
)
SELECT p.name AS player_name, l.team_name AS last_team,
       round(avg(g.fantasy_classic), 2) AS classic_per_game,
       count(g.game_code) AS games_played
FROM last_spell l
JOIN players p USING (person_code)
LEFT JOIN fantasy_points_game g
       ON g.person_code = l.person_code AND g.season_code = ?
WHERE l.person_code NOT IN (
      SELECT person_code FROM announced_rosters
      WHERE season_code = ? AND person_code <> ''
  )
GROUP BY 1, 2
ORDER BY classic_per_game DESC NULLS LAST
"""


def crawl_and_load(db_path: Path, cache_dir: Path, comp: str, season: str) -> int:
    from .http import ThrottledClient

    with ThrottledClient(cache_dir) as client:
        rows = fetch_rosters(client, comp, season)
    con = duckdb.connect(str(db_path))
    try:
        load(con, rows)
    finally:
        con.close()
    return len(rows)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Fetch announced rosters for a season")
    parser.add_argument("--competition", default="E", choices=["E", "U"])
    parser.add_argument("--season", required=True, help="e.g. E2026")
    parser.add_argument("--db", default="data/euroleague.duckdb", type=Path)
    parser.add_argument("--cache-dir", default="data/cache", type=Path)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    n = crawl_and_load(args.db, args.cache_dir, args.competition, args.season)
    print(f"{n} roster rows loaded for {args.season}")


if __name__ == "__main__":
    main()
