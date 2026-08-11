"""NBA production, converted into a EuroLeague fantasy expectation.

Roughly a third of every EuroLeague roster arrives from outside the competition, and for
those players this warehouse holds nothing at all. The NBA is where a large share of them
come from, so ignoring it means ranking them at null and letting the drafter guess.

Two claims were made against this earlier in the project and both were wrong.

"The API is blocked." A raw HTTP probe of stats.nba.com times out or is disconnected, and
cdn.nba.com returns 403, which is what produced that conclusion. The `nba_api` package
sends the header set the endpoint expects and gets an answer immediately.

"There is no honest way to fit a translation factor." That rested on a guess of ~10
movers. The real count of players who arrived in E2025 from outside the competition is
68 with 15+ games, and 16 of them matched an NBA season by name. Measured across those 16:

    NBA fantasy points  -> EuroLeague classic   r = +0.620
    NBA minutes         -> EuroLeague classic   r = +0.566
    median ratio 0.63, range 0.32 - 2.30

That correlation is stronger than the minute-pressure signal already shipped. It is still
16 players, so the factor is applied as a flat conversion with the source stated, never
blended into a EuroLeague number.

The ratio also runs the wrong way to intuition: fringe NBA players convert at 0.76 and
rotation players at 0.58. A man who played nine minutes a night in the NBA can start in
Europe, so his tiny NBA line understates him. This is why the range reaches 2.30 and why
no projection here should be read as a forecast for an individual.

Fetching runs at ETL time only. Nothing in the serving path imports this module.
"""

from __future__ import annotations

import logging
import unicodedata
from pathlib import Path
from typing import Any

import duckdb

log = logging.getLogger(__name__)

# Median EuroLeague-classic per NBA fantasy point across the 16 matched movers.
NBA_FACTOR = 0.63
NBA_MIN_GAMES = 20

# A simple, explicit NBA fantasy line. Not any particular site's formula -- it exists only
# to compress a box score into one number that the factor above was calibrated against,
# so it must stay exactly as it was when that measurement was taken.
NBA_WEIGHTS = {
    "PTS": 1.0, "REB": 1.2, "AST": 1.5, "STL": 2.0, "BLK": 2.0, "TOV": -1.0,
}


def normalise(name: str) -> str:
    """Strip accents and punctuation so two spellings of a name can meet."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().upper()
    return " ".join("".join(c for c in folded if c.isalpha() or c == " ").split())


def name_key(warehouse_name: str) -> tuple[str, str] | None:
    """The warehouse stores 'SURNAME, FORENAME'; the NBA stores 'Forename Surname'."""
    parts = normalise(warehouse_name.replace(",", " ")).split()
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


def fetch_season(season: str, timeout: int = 90) -> list[dict[str, Any]]:
    """Per-game NBA stats for one season, e.g. '2024-25'."""
    from nba_api.stats.endpoints import (  # type: ignore[import-untyped]
        leaguedashplayerstats,
    )

    frame = leaguedashplayerstats.LeagueDashPlayerStats(
        season=season, per_mode_detailed="PerGame", timeout=timeout
    ).get_data_frames()[0]

    rows: list[dict[str, Any]] = []
    for _, r in frame.iterrows():
        parts = normalise(r["PLAYER_NAME"]).split()
        if len(parts) < 2:
            continue
        fantasy = sum(float(r[k]) * w for k, w in NBA_WEIGHTS.items())
        rows.append(
            {
                "season": season,
                "player_name": r["PLAYER_NAME"],
                "surname": parts[-1],
                "forename": parts[0],
                "team": r["TEAM_ABBREVIATION"],
                "games_played": int(r["GP"]),
                "minutes_per_game": round(float(r["MIN"]), 1),
                "points_per_game": round(float(r["PTS"]), 1),
                "nba_fantasy_per_game": round(fantasy, 2),
                "euroleague_equivalent": round(fantasy * NBA_FACTOR, 2),
            }
        )
    log.info("NBA %s: %d players", season, len(rows))
    return rows


def load(con: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]]) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS nba_player_season (
            season VARCHAR, player_name VARCHAR, surname VARCHAR, forename VARCHAR,
            team VARCHAR, games_played INTEGER, minutes_per_game DOUBLE,
            points_per_game DOUBLE, nba_fantasy_per_game DOUBLE,
            euroleague_equivalent DOUBLE
        )
        """
    )
    if not rows:
        return
    con.execute("DELETE FROM nba_player_season WHERE season = ?", [rows[0]["season"]])
    con.executemany(
        "INSERT INTO nba_player_season VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [list(r.values()) for r in rows],
    )
    log.info("nba_player_season: %d rows for %s", len(rows), rows[0]["season"])


# Matched on name, because no identifier is shared between the two competitions. A surname
# shared by two NBA players is dropped rather than guessed -- picking one at random would
# put a stranger's season on someone's draft row.
NBA_FALLBACK_SQL = f"""
WITH candidate AS (
    -- Includes players with NO person code at all. They are the whole point: an NBA
    -- arrival has never appeared in this competition, so upstream has not issued him an
    -- identifier, and requiring one excluded exactly the 31 people this table exists for.
    SELECT a.person_code, a.player_name, a.position, a.team_code, a.team_name
    FROM announced_rosters a
    WHERE a.season_code = ?
      AND (a.person_code = '' OR a.person_code NOT IN (
              SELECT DISTINCT person_code FROM fantasy_points_game))
      AND a.player_name NOT IN (
          SELECT player_name FROM player_overrides
          WHERE status IN ('left_league', 'retired', 'unavailable'))
),
unique_nba AS (
    SELECT surname, forename, any_value(euroleague_equivalent) AS eq,
           any_value(nba_fantasy_per_game) AS raw, any_value(minutes_per_game) AS mpg,
           any_value(games_played) AS gp, count(*) AS n
    FROM nba_player_season
    WHERE season = ? AND games_played >= {NBA_MIN_GAMES}
    GROUP BY 1, 2
    HAVING count(*) = 1
)
SELECT c.person_code, c.player_name, c.position, c.team_code, c.team_name AS next_team,
       c.position AS next_position,
       n.eq AS value_per_game, n.raw AS source_value, n.gp AS games_played,
       'NBA ' || ? AS source_season,
       'NBA ' || ? || ' x {NBA_FACTOR}' AS value_source
FROM candidate c
JOIN unique_nba n
  ON n.surname = split_part(upper(c.player_name), ',', 1)
 AND n.forename = trim(split_part(upper(c.player_name), ',', 2))
"""


def crawl_and_load(db_path: Path, season: str) -> int:
    rows = fetch_season(season)
    con = duckdb.connect(str(db_path))
    try:
        load(con, rows)
    finally:
        con.close()
    return len(rows)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Fetch an NBA season for cross-reference")
    parser.add_argument("--season", default="2025-26", help="e.g. 2025-26")
    parser.add_argument("--db", default="data/euroleague.duckdb", type=Path)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    n = crawl_and_load(args.db, args.season)
    print(f"{n} NBA rows loaded for {args.season}")


if __name__ == "__main__":
    main()
