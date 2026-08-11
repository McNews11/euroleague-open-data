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
