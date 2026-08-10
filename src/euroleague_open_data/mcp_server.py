"""MCP server over the EuroLeague warehouse.

This module deliberately does not import httpx or anything from the fetch layer. The
server reads the local DuckDB file and nothing else -- no request from an LLM client can
ever reach the upstream API. That is not a performance choice: the upstream rate-limits at
roughly 10 requests/minute across all callers, so a proxying server would let one curious
user black out everybody else.

Tool descriptions are written for the model, not for a human reader. They are the prompt
that decides whether the right tool gets picked.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

import duckdb

# MCP Python SDK 2.0 renamed FastMCP to MCPServer and moved it out of mcp.server.fastmcp.
# The decorator API is unchanged, and .run() gained a `transport` argument, which is what
# the future HTTP deployment will use.
from mcp.server.mcpserver import MCPServer

log = logging.getLogger(__name__)

MAX_ROWS = 500
QUERY_TIMEOUT_SECONDS = 15.0

FORBIDDEN_SQL = (
    "insert",
    "update",
    "delete",
    "drop",
    "create",
    "alter",
    "attach",
    "detach",
    "copy",
    "install",
    "load",
    "pragma",
    "export",
    "import",
    "call",
    "set",
)

mcp = MCPServer(
    "euroleague-open-data",
    instructions=(
        "Unofficial EuroLeague/EuroCup basketball warehouse. Data originates from "
        "Euroleague Basketball and is served from a local snapshot -- never live. "
        "Resolve names to ids with search_players/search_teams before calling other "
        "tools. Check the euroleague://coverage resource before claiming data is "
        "missing, and never estimate a number the warehouse does not contain."
    ),
)


def _db_path() -> Path:
    env = os.environ.get("EUROLEAGUE_DB")
    if env:
        return Path(env)
    return Path.cwd() / "data" / "euroleague.duckdb"


_connection: duckdb.DuckDBPyConnection | None = None
_lock = threading.Lock()


def _con() -> duckdb.DuckDBPyConnection:
    global _connection
    if _connection is None:
        path = _db_path()
        if not path.exists():
            raise FileNotFoundError(
                f"warehouse not found at {path}. Set EUROLEAGUE_DB or build it with "
                "`euroleague-etl --season E2025`."
            )
        _connection = duckdb.connect(str(path), read_only=True)
    return _connection


def _query(sql: str, params: list[Any] | None = None, *, limit: int = MAX_ROWS) -> dict[str, Any]:
    """Run a read-only query with a row cap and a hard timeout."""
    con = _con()
    with _lock:
        timer = threading.Timer(QUERY_TIMEOUT_SECONDS, con.interrupt)
        timer.start()
        try:
            cursor = con.execute(sql, params or [])
            rows = cursor.fetchmany(limit)
            columns = [d[0] for d in cursor.description or []]
            truncated = len(cursor.fetchmany(1)) > 0
        except duckdb.InterruptException:
            return {"error": f"query exceeded {QUERY_TIMEOUT_SECONDS:.0f}s and was cancelled"}
        except duckdb.Error as exc:
            return {"error": str(exc)}
        finally:
            timer.cancel()

    return {
        "columns": columns,
        "rows": [dict(zip(columns, r, strict=True)) for r in rows],
        "row_count": len(rows),
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------- tools


@mcp.tool()
def search_players(name: str, season: str | None = None, limit: int = 10) -> dict[str, Any]:
    """Find a player's canonical person_code from a partial or misspelled name.

    Call this FIRST whenever the user names a player. Every other player tool takes a
    person_code, not a name. Matching is case-insensitive and substring-based, so
    "doncic", "Luka", and "DONCIC, LUKA" all work.

    Args:
        name: any part of the player's name.
        season: optional season code such as "E2025" to restrict to players active then.
        limit: maximum results, default 10.
    """
    sql = """
        SELECT DISTINCT p.person_code, p.name, p.country_name, p.height_cm,
               p.birth_date, p.position_name,
               (SELECT string_agg(DISTINCT s.team_code, ', ')
                  FROM player_season_stats s
                 WHERE s.person_code = p.person_code) AS teams
        FROM players p
        WHERE lower(p.name) LIKE lower('%' || ? || '%')
    """
    params: list[Any] = [name]
    if season:
        sql += """ AND EXISTS (SELECT 1 FROM player_season_stats s
                    WHERE s.person_code = p.person_code AND s.season_code = ?)"""
        params.append(season)
    sql += " ORDER BY p.name LIMIT ?"
    params.append(min(limit, 50))
    return _query(sql, params)


@mcp.tool()
def search_teams(name: str, season: str | None = None) -> dict[str, Any]:
    """Find a club's canonical team_code from a partial name.

    Clubs are renamed by sponsors between seasons ("Kosner Baskonia" vs "Baskonia"), so
    match against both the seasonal name and the permanent club name.

    Args:
        name: any part of the club name, e.g. "zalgiris", "real", "efes".
        season: optional season code such as "E2025".
    """
    sql = """
        SELECT DISTINCT team_code, name, club_permanent_name, country_name, season_code
        FROM teams
        WHERE lower(name) LIKE lower('%' || ? || '%')
           OR lower(coalesce(club_permanent_name, '')) LIKE lower('%' || ? || '%')
           OR lower(team_code) = lower(?)
    """
    params: list[Any] = [name, name, name]
    if season:
        sql += " AND season_code = ?"
        params.append(season)
    sql += " ORDER BY season_code DESC, name LIMIT 50"
    return _query(sql, params)


@mcp.tool()
def get_player_stats(
    person_code: str,
    season: str | None = None,
    per_mode: str = "total",
) -> dict[str, Any]:
    """Season stats for one player: traditional totals plus true shooting, eFG and usage.

    Requires a person_code from search_players. Returns one row per season the player
    appears in, so omit `season` to get a career view.

    Args:
        person_code: canonical id from search_players, e.g. "006590".
        season: optional season code such as "E2025".
        per_mode: "total", "per_game", or "per_36". Rate stats (true_shooting_pct,
            efg_pct, usage_pct) are identical in all three modes.
    """
    base = [
        "season_code",
        "person_code",
        "player_name",
        "team_code",
        "games_played",
        "minutes",
    ]
    rates = ["true_shooting_pct", "efg_pct", "fg_pct", "fg3_pct", "ft_pct", "usage_pct"]

    if per_mode == "per_game":
        counting = ["points_per_game", "rebounds_per_game", "assists_per_game", "minutes_per_game"]
    elif per_mode == "per_36":
        counting = ["points_per_36", "rebounds_per_36", "assists_per_36"]
    else:
        counting = [
            "points",
            "fg2m",
            "fg2a",
            "fg3m",
            "fg3a",
            "ftm",
            "fta",
            "rebounds_offensive",
            "rebounds_defensive",
            "rebounds_total",
            "assists",
            "steals",
            "turnovers",
            "blocks",
            "valuation",
            "plus_minus",
        ]

    columns = ", ".join(base + counting + rates)
    sql = f"SELECT {columns} FROM player_season_stats WHERE person_code = ?"
    params: list[Any] = [person_code]
    if season:
        sql += " AND season_code = ?"
        params.append(season)
    sql += " ORDER BY season_code DESC"
    return _query(sql, params)


@mcp.tool()
def get_team_stats(team_code: str, season: str | None = None) -> dict[str, Any]:
    """Team season statistics including offensive/defensive rating and the Four Factors.

    Four Factors are returned for the team and for its opponents, which is what makes them
    interpretable: a good `tov_pct` is low, a good `opp_tov_pct` is high.

    Args:
        team_code: canonical code from search_teams, e.g. "MAD", "ZAL".
        season: optional season code such as "E2025".
    """
    sql = "SELECT * FROM team_season_stats WHERE team_code = ?"
    params: list[Any] = [team_code]
    if season:
        sql += " AND season_code = ?"
        params.append(season)
    sql += " ORDER BY season_code DESC"
    return _query(sql, params)


@mcp.tool()
def get_game_boxscore(season: str, game_code: int) -> dict[str, Any]:
    """Full boxscore for one game: final score, both team totals, and every player line.

    Args:
        season: season code such as "E2025".
        game_code: the game's numeric code within that season.
    """
    game = _query(
        """SELECT game_code, round, phase_name, utc_date, home_team_code, away_team_code,
                  home_score, away_score, home_q1, home_q2, home_q3, home_q4,
                  away_q1, away_q2, away_q3, away_q4, venue_name, attendance,
                  referee_1, referee_2, referee_3
           FROM games WHERE season_code = ? AND game_code = ?""",
        [season, game_code],
    )
    players = _query(
        """SELECT b.team_code, b.is_home, p.name AS player_name, b.dorsal,
                  round(b.seconds_played/60.0, 1) AS minutes, b.points,
                  b.fg2m, b.fg2a, b.fg3m, b.fg3a, b.ftm, b.fta,
                  b.rebounds_offensive, b.rebounds_defensive, b.rebounds_total,
                  b.assists, b.steals, b.turnovers, b.blocks_favour,
                  b.fouls_committed, b.valuation, b.plus_minus
           FROM boxscores_player b
           LEFT JOIN players p USING (person_code)
           WHERE b.season_code = ? AND b.game_code = ?
           ORDER BY b.is_home DESC, b.seconds_played DESC""",
        [season, game_code],
    )
    teams = _query(
        "SELECT * FROM boxscores_team WHERE season_code = ? AND game_code = ?",
        [season, game_code],
    )
    completeness = _query(
        "SELECT * FROM game_completeness WHERE season_code = ? AND game_code = ?",
        [season, game_code],
    )
    return {
        "game": game.get("rows"),
        "team_totals": teams.get("rows"),
        "players": players.get("rows"),
        "completeness": completeness.get("rows"),
    }


@mcp.tool()
def get_shot_chart(
    season: str,
    person_code: str | None = None,
    team_code: str | None = None,
    include_raw: bool = False,
) -> dict[str, Any]:
    """Shot-zone breakdown, optionally with raw x/y coordinates.

    Shot data exists from the 2007 season onward only; earlier seasons have boxscores but
    no coordinates. Zone letters are upstream's own coding and the human-readable names
    are provisional -- see the data-quality resource.

    Args:
        season: season code such as "E2025".
        person_code: optional player id from search_players.
        team_code: optional club code from search_teams.
        include_raw: also return individual shots with coordinates. Capped at 500 rows.
    """
    sql = """SELECT zone, zone_name, sum(attempts) AS attempts, sum(makes) AS makes,
                    round(sum(makes)*1.0/nullif(sum(attempts),0), 4) AS fg_pct,
                    sum(points) AS points
             FROM shot_zones WHERE season_code = ?"""
    params: list[Any] = [season]
    if person_code:
        sql += " AND person_code = ?"
        params.append(person_code)
    if team_code:
        sql += " AND team_code = ?"
        params.append(team_code)
    sql += " GROUP BY zone, zone_name ORDER BY attempts DESC"
    result: dict[str, Any] = {"zones": _query(sql, params).get("rows")}

    if include_raw:
        raw_sql = """SELECT game_code, person_code, player_name, team_code, action_id,
                            points, coord_x, coord_y, zone, minute, is_fastbreak,
                            is_second_chance
                     FROM shots WHERE season_code = ?"""
        raw_params: list[Any] = [season]
        if person_code:
            raw_sql += " AND person_code = ?"
            raw_params.append(person_code)
        if team_code:
            raw_sql += " AND team_code = ?"
            raw_params.append(team_code)
        raw_sql += " ORDER BY game_code, sequence"
        result["shots"] = _query(raw_sql, raw_params)

    return result


@mcp.tool()
def run_sql(sql: str, limit: int = 100) -> dict[str, Any]:
    """Run a read-only SQL SELECT against the warehouse.

    Use this for any question the other tools cannot answer.

    Read the `euroleague://schema` resource first to see table and column names.

    Only a single SELECT or WITH statement is permitted. The connection is read-only,
    results are capped, and queries are cancelled after 15 seconds.

    Args:
        sql: a single SELECT (or WITH ... SELECT) statement.
        limit: maximum rows to return, capped at 500.
    """
    cleaned = sql.strip().rstrip(";").strip()
    lowered = cleaned.lower()

    if not (lowered.startswith("select") or lowered.startswith("with")):
        return {"error": "only SELECT or WITH statements are allowed"}
    if ";" in cleaned:
        return {"error": "only a single statement is allowed"}
    for word in FORBIDDEN_SQL:
        if f" {word} " in f" {lowered} " or lowered.startswith(f"{word} "):
            return {"error": f"statement contains forbidden keyword: {word}"}

    return _query(cleaned, limit=min(limit, MAX_ROWS))


# -------------------------------------------------------------------------- fantasy


@mcp.tool()
def get_draft_board(
    season: str = "E2025",
    teams: int = 8,
    roster_size: int = 13,
    position: str | None = None,
    min_games: int = 5,
    limit: int = 40,
) -> dict[str, Any]:
    """Rank players for a BasketNews Fantasy DRAFT by value over replacement.

    Use this for "who should I pick", "best available guard", or any draft ordering
    question. Do NOT rank by points per game for a draft: every manager gets a unique
    roster, so what matters is how much better a player is than the next player at the
    SAME position who will still be available. That is `vorp_per_game`, and it is the
    correct sort order.

    Scoring is the BasketNews modern system, recomputed exactly from boxscores.

    Fields worth reasoning about:
      vorp_per_game     - value over replacement. The draft ranking.
      modern_per_game   - raw fantasy average.
      modern_floor_p25  - bad-night floor. Matters more in a draft than in a budget
                          league, because you keep the pick all season.
      consistency_ratio - mean divided by standard deviation. Higher is steadier.
      replacement_level - what is still gettable at this position late in the draft.

    Args:
        season: season code, e.g. "E2025".
        teams: managers in the league, 3-12. This changes replacement level and therefore
            the ranking, so ask the user if it is unknown. BasketNews recommends 7-8.
        roster_size: players per roster. BasketNews draft mode is 13.
        position: optional filter, "Guard", "Forward" or "Center".
        min_games: exclude players below this many appearances.
        limit: rows to return.
    """
    from .fantasy import draft_board_select

    if not 3 <= teams <= 12:
        return {"error": "BasketNews draft leagues have between 3 and 12 teams"}

    inner = draft_board_select(teams, roster_size)
    sql = f"""
        SELECT overall_rank, pos_rank, player_name, position, team_code, games_played,
               minutes_per_game, modern_per_game, modern_floor_p25, modern_ceiling_p75,
               consistency_ratio, double_doubles, replacement_level, vorp_per_game,
               vorp_total, classic_per_game
        FROM ({inner}) board
        WHERE season_code = ? AND games_played >= ?
    """
    params: list[Any] = [season, min_games]
    if position:
        sql += " AND lower(position) = lower(?)"
        params.append(position)
    sql += " ORDER BY vorp_per_game DESC LIMIT ?"
    params.append(min(limit, MAX_ROWS))

    result = _query(sql, params)
    result["scoring_system"] = "BasketNews modern (draft mode)"
    result["league"] = {"teams": teams, "roster_size": roster_size}
    return result


@mcp.tool()
def get_player_fantasy_log(
    person_code: str,
    season: str = "E2025",
    last_n: int = 0,
) -> dict[str, Any]:
    """Game-by-game fantasy points for one player, for judging form and reliability.

    A season average hides the thing that decides drafts: whether a player's role changed.
    Someone averaging 20 who went 8, 9, 10, then 35, 38, 40 is a different asset from
    someone who scored 20 every night. Read the sequence, not only the mean.

    Args:
        person_code: canonical id from search_players.
        season: season code, e.g. "E2025".
        last_n: return only the most recent N games. 0 returns the whole season.
    """
    sql = """
        SELECT round, utc_date, team_code, minutes, points, rebounds_total,
               assists, steals, turnovers, missed_fg, pir, team_won,
               fantasy_modern, fantasy_classic
        FROM fantasy_points_game
        WHERE person_code = ? AND season_code = ? AND minutes > 0
        ORDER BY utc_date DESC
    """
    params: list[Any] = [person_code, season]
    if last_n > 0:
        sql += " LIMIT ?"
        params.append(min(last_n, MAX_ROWS))
    return _query(sql, params)


@mcp.tool()
def compare_draft_candidates(
    person_codes: list[str],
    season: str = "E2025",
    teams: int = 8,
) -> dict[str, Any]:
    """Compare named players side by side for a draft pick decision.

    Use when the user is choosing between specific players ("Vezenkov or Milutinov?").
    Resolve names to person_codes with search_players first.

    Args:
        person_codes: two or more canonical ids.
        season: season code, e.g. "E2025".
        teams: managers in the league, used to set replacement level.
    """
    from .fantasy import draft_board_select

    if len(person_codes) < 2:
        return {"error": "give at least two person_codes to compare"}

    inner = draft_board_select(teams, 13)
    placeholders = ", ".join("?" for _ in person_codes)
    sql = f"""
        WITH board AS ({inner})
        SELECT f.person_code, b.player_name, b.position, b.team_code, b.games_played,
               b.minutes_per_game, b.modern_per_game, b.modern_floor_p25,
               b.modern_ceiling_p75, b.consistency_ratio, b.vorp_per_game,
               b.pos_rank, b.overall_rank
        FROM board b
        JOIN fantasy_player_season f
          ON f.person_code = b.person_code AND f.season_code = b.season_code
        WHERE f.person_code IN ({placeholders}) AND b.season_code = ?
        ORDER BY b.vorp_per_game DESC
    """
    return _query(sql, [*person_codes, season])


# ------------------------------------------------------------------------ resources


@mcp.resource("euroleague://schema")
def schema() -> str:
    """DDL for every table in the warehouse. Read this before writing SQL with run_sql."""
    con = _con()
    rows = con.execute(
        """SELECT table_name, column_name, data_type
           FROM information_schema.columns
           WHERE table_schema = 'main'
           ORDER BY table_name, ordinal_position"""
    ).fetchall()

    out: list[str] = []
    current = None
    for table, column, dtype in rows:
        if table != current:
            if current is not None:
                out.append("")
            count = con.execute(f"SELECT count(*) FROM {table}").fetchone()
            out.append(f"TABLE {table}  ({count[0] if count else '?'} rows)")
            current = table
        out.append(f"  {column:<26} {dtype}")
    return "\n".join(out)


@mcp.resource("euroleague://coverage")
def coverage() -> str:
    """Which seasons and competitions are loaded, and how complete each game is."""
    con = _con()
    rows = con.execute(
        """SELECT g.season_code,
                  count(*) AS games,
                  count(*) FILTER (WHERE c.has_boxscore) AS with_boxscore,
                  count(*) FILTER (WHERE c.has_shots) AS with_shots,
                  count(*) FILTER (WHERE c.has_play_by_play) AS with_pbp,
                  count(*) FILTER (WHERE c.lineup_safe) AS lineup_safe
           FROM games g
           LEFT JOIN game_completeness c
             ON c.season_code = g.season_code AND c.game_code = g.game_code
           WHERE g.played
           GROUP BY 1 ORDER BY 1"""
    ).fetchall()

    lines = [
        "Loaded coverage. A game missing from `with_shots` has no shot chart -- say so",
        "rather than estimating. `lineup_safe` counts games whose play-by-play period",
        "ordering is self-consistent; the rest are unreliable for lineup work.",
        "",
        f"{'season':<10}{'games':>7}{'boxscore':>10}{'shots':>8}{'pbp':>7}{'lineup_safe':>13}",
    ]
    for r in rows:
        lines.append(f"{r[0]:<10}{r[1]:>7}{r[2]:>10}{r[3]:>8}{r[4]:>7}{r[5]:>13}")
    return "\n".join(lines)


@mcp.resource("euroleague://data-quality")
def data_quality() -> str:
    """The most recent validation report: which reconciliation checks passed and failed."""
    report = Path(__file__).resolve().parents[2] / "docs" / "data-quality-report.json"
    if not report.exists():
        return "No validation report has been generated yet."
    return report.read_text()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    mcp.run()


if __name__ == "__main__":
    main()
