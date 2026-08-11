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
from importlib import metadata
from pathlib import Path
from typing import Any

import duckdb

# MCP Python SDK 2.0 renamed FastMCP to MCPServer and moved it out of mcp.server.fastmcp.
# The decorator API is unchanged, and .run() gained a `transport` argument, which is what
# the future HTTP deployment will use.
from mcp.server.mcpserver import MCPServer

from . import nba, rosters

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

def _version() -> str:
    """Reported to clients on connect, so a stale deployment can be identified."""
    try:
        return metadata.version("euroleague-open-data")
    except metadata.PackageNotFoundError:  # running from a source tree without an install
        return "0.0.0+dev"


mcp = MCPServer(
    "euroleague-open-data",
    version=_version(),
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
        # Free hosting tiers are memory-capped, and an OOM kill takes down the whole
        # container: every other user's request dies so one run_sql could try to hash
        # 625k play-by-play rows. A budget makes DuckDB spill to disk, or fail that one
        # query with a real error, instead. Tuned by DUCKDB_MEMORY_LIMIT.
        limit = os.environ.get("DUCKDB_MEMORY_LIMIT", "256MB")
        _connection.execute(f"SET memory_limit = '{limit}'")
        _connection.execute("SET threads = 2")
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
    scoring: str = "classic",
    next_season: str = "E2026",
    adjust_for_minutes: bool = True,
    second_tier: str = "U2025",
    nba_season: str = "2025-26",
) -> dict[str, Any]:
    """Rank players for a BasketNews Fantasy DRAFT by value over replacement.

    Use this for "who should I pick", "best available guard", or any draft ordering
    question. Do NOT rank by points per game for a draft: every manager gets a unique
    roster, so what matters is how much better a player is than the next player at the
    SAME position who will still be available. That is `vorp_per_game`, and it is the
    correct sort order.

    Scoring is recomputed exactly from boxscores.

    Two things make last season's ranking misleading in August, and both are corrected
    when `next_season` has announced rosters loaded.

    Players with no club yet are removed. Six of them sat inside the naive top 60 --
    Angola 14th, Lyles 23rd, Mirotic, Hezonja, Osman. They are undraftable, and anyone
    ranking on last season's numbers will burn picks on them.

    `minute_pressure` is what the club's announced squad is used to playing at that
    position over what the club actually gave it last season. High pressure means the
    player has to win minutes he did not have to win before -- which is the mechanism
    behind a good player scoring less at a new club. Backtested E2024->E2025 (n=149):
    70% of high-pressure players declined against 27% of low-pressure ones, correlation
    -0.28. Real, but weak: treat fifty places as signal and ten as noise, and never
    present the adjusted number as a forecast.

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
        next_season: season being drafted. When its announced rosters are loaded, the
            board is restricted to players who actually have a club and each one carries
            the minute pressure at his new position. Pass "" to rank on last season alone.
        adjust_for_minutes: sort by the pressure-adjusted value rather than raw VORP.
    """
    from .fantasy import draft_board_select

    if not 3 <= teams <= 12:
        return {"error": "BasketNews draft leagues have between 3 and 12 teams"}

    inner = draft_board_select(teams, roster_size, scoring)
    value = "classic_per_game" if scoring == "classic" else "modern_per_game"

    have_rosters = False
    if next_season:
        check = _query(
            "SELECT count(*) AS n FROM announced_rosters WHERE season_code = ?",
            [next_season],
        )
        rows = check.get("rows") or []
        have_rosters = bool(rows and rows[0].get("n"))

    if not have_rosters:
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
        result["scoring_system"] = f"BasketNews {scoring} (draft mode)"
        result["league"] = {"teams": teams, "roster_size": roster_size}
        if next_season:
            result["minute_pressure"] = (
                f"not applied: no announced rosters loaded for {next_season}. This is "
                "'unknown', not 'no competition for minutes'."
            )
        return result

    sql = f"""
        WITH board AS (SELECT * FROM ({inner}) b WHERE b.season_code = ?),
        pressure AS ({rosters.PRESSURE_SQL})
        SELECT b.overall_rank AS raw_rank, b.player_name, b.position, b.team_code,
               b.games_played, b.minutes_per_game, b.{value} AS value_per_game,
               'EuroLeague 2025-26' AS value_source,
               b.vorp_per_game, b.consistency_ratio,
               pr.next_team, pr.next_position, pr.minute_pressure,
               CASE WHEN pr.minute_pressure >= {rosters.PRESSURE_HIGH} THEN 'high'
                    WHEN pr.minute_pressure <= {rosters.PRESSURE_LOW} THEN 'low'
                    ELSE 'normal' END AS pressure_band,
               round(b.{value} - CASE WHEN pr.minute_pressure >= {rosters.PRESSURE_HIGH}
                                      THEN {rosters.PRESSURE_PENALTY} ELSE 0 END, 2)
                   AS adjusted_per_game
        FROM board b
        JOIN pressure pr USING (person_code)
        WHERE b.games_played >= ?
          AND b.person_code NOT IN (
              SELECT person_code FROM player_overrides WHERE status IN ('left_league',
                     'retired', 'unavailable'))
    """
    params = [season, *rosters.pressure_params(season, next_season), min_games]
    if position:
        sql += " AND lower(b.position) = lower(?)"
        params.append(position)

    if second_tier:
        # Same shape, same pressure penalty, but the value came from the other
        # competition and is labelled so nobody reads it as a like-for-like number.
        sql += f"""
        UNION ALL
        SELECT NULL AS raw_rank, s.player_name, s.position, NULL AS team_code,
               s.games_played, NULL AS minutes_per_game, s.value_per_game,
               s.value_source,
               NULL AS vorp_per_game, NULL AS consistency_ratio,
               s.next_team, s.next_position, pr2.minute_pressure,
               CASE WHEN pr2.minute_pressure >= {rosters.PRESSURE_HIGH} THEN 'high'
                    WHEN pr2.minute_pressure <= {rosters.PRESSURE_LOW} THEN 'low'
                    ELSE 'normal' END AS pressure_band,
               round(s.value_per_game - CASE WHEN pr2.minute_pressure >= {rosters.PRESSURE_HIGH}
                                             THEN {rosters.PRESSURE_PENALTY} ELSE 0 END, 2)
                   AS adjusted_per_game
        FROM ({rosters.EUROCUP_FALLBACK_SQL}) s
        LEFT JOIN ({rosters.PRESSURE_SQL}) pr2 USING (person_code)
        """
        params += [next_season, season]
        params += rosters.pressure_params(season, next_season)

    if nba_season:
        # Lowest priority: only players with no European history at all. Same shape, same
        # pressure penalty, and the source says NBA so nothing reads like a EuroLeague
        # figure. Calibrated on 16 movers, r = +0.62 -- real, and thin.
        sql += f"""
        UNION ALL
        SELECT NULL AS raw_rank, s.player_name, s.position, NULL AS team_code,
               s.games_played, NULL AS minutes_per_game, s.value_per_game,
               s.value_source,
               NULL AS vorp_per_game, NULL AS consistency_ratio,
               s.next_team, s.next_position, pr3.minute_pressure,
               CASE WHEN pr3.minute_pressure >= {rosters.PRESSURE_HIGH} THEN 'high'
                    WHEN pr3.minute_pressure <= {rosters.PRESSURE_LOW} THEN 'low'
                    ELSE 'normal' END AS pressure_band,
               round(s.value_per_game - CASE WHEN pr3.minute_pressure >= {rosters.PRESSURE_HIGH}
                                             THEN {rosters.PRESSURE_PENALTY} ELSE 0 END, 2)
                   AS adjusted_per_game
        FROM ({nba.NBA_FALLBACK_SQL}) s
        LEFT JOIN (SELECT DISTINCT next_team, next_position, minute_pressure
                   FROM ({rosters.PRESSURE_SQL})) pr3
               ON pr3.next_team = s.next_team AND pr3.next_position = s.next_position
        """
        params += [next_season, nba_season, nba_season, nba_season]
        params += rosters.pressure_params(season, next_season)

    sql = f"SELECT * FROM ({sql})"
    sql += (
        f" ORDER BY {'adjusted_per_game' if adjust_for_minutes else 'vorp_per_game'}"
        " DESC NULLS LAST LIMIT ?"
    )
    params.append(min(limit, MAX_ROWS))

    result = _query(sql, params)
    result["scoring_system"] = f"BasketNews {scoring} (draft mode)"
    result["league"] = {"teams": teams, "roster_size": roster_size}
    result["filtered_to"] = (
        f"players on an announced {next_season} roster. Anyone still unsigned is absent "
        "from this list -- use get_transfers(status='unsigned') to see them."
    )
    result["minute_pressure"] = {
        "definition": "squad's previous workload at that position / minutes the club "
                      "actually gave that position last season",
        "bands": f"low <= {rosters.PRESSURE_LOW}, high >= {rosters.PRESSURE_HIGH}, "
                 "league median 1.11",
        "penalty_applied": rosters.PRESSURE_PENALTY if adjust_for_minutes else 0,
        "evidence": "backtested E2024->E2025, n=149: 70% of high-pressure players "
                    "declined against 27% of low-pressure ones, correlation -0.28",
        "caution": "weak signal. Ten places apart on this board is noise; fifty is not.",
    }
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
    scoring: str = "classic",
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

    inner = draft_board_select(teams, 13, scoring)
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


@mcp.tool()
def plan_snake_draft(
    pick_slot: int,
    season: str = "E2025",
    teams: int = 8,
    rounds: int = 13,
    reverse_snake: bool = False,
    scoring: str = "classic",
) -> dict[str, Any]:
    """Work out which overall picks you own and who should be there when your turn comes.

    Use this when the user knows their draft slot and wants a plan ("I pick 3rd of 8,
    what should I target?").

    Snake order: odd rounds run 1..N, even rounds run N..1, so a late slot gets a fast
    turnaround between picks and an early slot waits. BasketNews also offers a reverse
    snake, where rounds 1 and 2 are the normal snake and the direction then repeats in
    pairs; set reverse_snake for that.

    `likely_available` assumes every manager drafts strictly off this board, which nobody
    does. Treat it as the centre of a distribution, not a prediction. Its real use is
    spotting where a positional tier runs out between two of your picks -- that is the
    signal worth acting on.

    Args:
        pick_slot: your position in round one, 1 to `teams`.
        season: season code, e.g. "E2025".
        teams: managers in the league.
        rounds: roster size. BasketNews draft mode is 13.
        reverse_snake: use BasketNews reverse-snake order instead of standard snake.
        scoring: "classic" or "modern". BasketNews leagues choose one; ask the user.
    """
    from .fantasy import draft_board_select

    if not 1 <= pick_slot <= teams:
        return {"error": f"pick_slot must be between 1 and {teams}"}
    if not 3 <= teams <= 12:
        return {"error": "BasketNews draft leagues have between 3 and 12 teams"}

    picks: list[dict[str, Any]] = []
    for rnd in range(1, rounds + 1):
        # Reverse snake pairs the rounds up: 1 forward, 2 and 3 reverse, 4 and 5
        # forward, and so on. Standard snake simply alternates.
        forward = ((rnd + 1) // 2) % 2 == 1 if reverse_snake else rnd % 2 == 1
        position = pick_slot if forward else teams - pick_slot + 1
        picks.append(
            {
                "round": rnd,
                "pick_in_round": position,
                "overall_pick": (rnd - 1) * teams + position,
            }
        )

    inner = draft_board_select(teams, rounds, scoring)
    board = _query(
        f"""SELECT overall_rank, player_name, position, team_code, games_played,
                   minutes_per_game, vorp_per_game, modern_floor_p25, consistency_ratio
            FROM ({inner}) b
            WHERE season_code = ?
            ORDER BY vorp_per_game DESC
            LIMIT ?""",
        [season, teams * rounds],
    )
    ranked = board.get("rows") or []

    for pick in picks:
        index = pick["overall_pick"] - 1
        pick["likely_available"] = ranked[index] if index < len(ranked) else None

    return {
        "your_picks": picks,
        "gap_between_picks": [
            picks[i + 1]["overall_pick"] - picks[i]["overall_pick"] for i in range(len(picks) - 1)
        ],
        "scoring_system": scoring,
        "league": {"teams": teams, "rounds": rounds, "reverse_snake": reverse_snake},
        "note": (
            "likely_available assumes everyone drafts off this exact board. Use it to see "
            "where a position thins out between your picks, not as a forecast of who falls."
        ),
    }


@mcp.tool()
def get_coach_rotation(
    season: str = "E2025",
    team_code: str | None = None,
    coach_name: str | None = None,
) -> dict[str, Any]:
    """How a coach distributes minutes. Use this to judge a player's minutes ceiling.

    Rotation depth is the strongest lever on fantasy output that is not the player
    himself: the same player scores more under a coach who plays nine men heavy minutes
    than under one who rides seven. Rotation habits travel with the coach between clubs,
    so this is keyed on the coach, not the team.

    `rotation_style` is a tercile RELATIVE TO THIS COMPETITION, not an absolute standard.
    Coaches with fewer than 10 games are labelled `insufficient_data` rather than guessed
    at. `minute_concentration` is a Herfindahl index of minute shares: higher means
    minutes are concentrated in fewer players.

    Args:
        season: season code, e.g. "E2025".
        team_code: optional club code from search_teams.
        coach_name: optional partial coach name.
    """
    sql = """
        SELECT coach_name, team_code, games_coached, avg_players_used,
               avg_players_15plus, avg_players_20plus, avg_top_minutes,
               minute_concentration, rotation_style
        FROM coach_rotation_profile
        WHERE season_code = ?
    """
    params: list[Any] = [season]
    if team_code:
        sql += " AND team_code = ?"
        params.append(team_code)
    if coach_name:
        sql += " AND lower(coach_name) LIKE lower('%' || ? || '%')"
        params.append(coach_name)
    sql += " ORDER BY avg_players_15plus DESC"
    return _query(sql, params)


@mcp.tool()
def get_role_outlook(team_code: str, season: str = "E2025") -> dict[str, Any]:
    """What minutes and production a club has vacated, by position, plus who remains.

    This is the tool for "how will player X do at his new club" and for drafting anyone
    without history in this competition.

    Be honest about what this can and cannot do. If a player arrives from the NBA or a
    domestic league, this warehouse holds ZERO rows for him and no projection is possible
    from it. What IS knowable is the role he is walking into: the minutes and fantasy
    production the club lost at his position, and how stable the surviving players' minutes
    are. State the vacated role, state that the player's own level is an input you do not
    have, and let the user supply it. Do not invent a projection.

    Returned per position: vacated minutes and fantasy points per game from players whose
    roster spell has ended, alongside the remaining players' minute stability.

    Args:
        team_code: club code from search_teams.
        season: season code, e.g. "E2025".
    """
    vacated = _query(
        """SELECT position_name, departed_players, vacated_minutes_per_game,
                  vacated_fantasy_per_game, avg_departed_minutes,
                  biggest_departure_minutes
           FROM vacated_role
           WHERE team_code = ? AND season_code = ?
           ORDER BY vacated_minutes_per_game DESC""",
        [team_code, season],
    )
    vacated_rows = vacated.get("rows") or []
    remaining = _query(
        """SELECT r.player_name, r.position_name, r.games_played, r.availability,
                  r.minutes_per_game, r.minutes_stddev, r.team_minute_share,
                  s.is_active, s.previous_team
           FROM player_role_stability r
           LEFT JOIN player_team_spells s
             ON s.person_code = r.person_code AND s.season_code = r.season_code
           WHERE r.team_code = ? AND r.season_code = ?
           ORDER BY r.minutes_per_game DESC""",
        [team_code, season],
    )
    coach = _query(
        """SELECT coach_name, games_coached, avg_players_15plus, rotation_style
           FROM coach_rotation_profile WHERE team_code = ? AND season_code = ?
           ORDER BY games_coached DESC""",
        [team_code, season],
    )
    result: dict[str, Any] = {
        "vacated_by_position": vacated_rows,
        "roster_minute_stability": remaining.get("rows"),
        "coaching": coach.get("rows"),
        "caveat": (
            "Players arriving from outside this competition have no rows here. "
            "Use the vacated role as the opportunity estimate; the player's own level "
            "must come from outside this dataset."
        ),
    }
    if not vacated_rows:
        result["vacated_unavailable"] = (
            f"No departure data for {season}. Vacated minutes are computed by comparing a "
            "club's roster against the previous season, so the season before this one must "
            "also be loaded. Say this plainly rather than reporting zero departures -- zero "
            "loaded seasons and zero departures are different facts."
        )
    return result


@mcp.tool()
def get_transfers(
    season: str = "E2026",
    previous_season: str = "E2025",
    team_code: str = "",
    status: str = "moved",
    min_classic: float = 0.0,
    limit: int = 60,
) -> dict[str, Any]:
    """Where players actually are for an upcoming season, and who changed club.

    This is the tool for drafting before a ball has been thrown up. Every other tool here
    is built from games that have been played, so none of them know that a player signed
    somewhere new in the summer. This one reads the clubs' announced squads.

    Transfers are detected by comparing club CODES, never names. Sponsors change every
    summer -- Maccabi Playtika became Maccabi Rapyd, EA7 Emporio Armani became Armani
    Olimpia -- and matching on names reports an entire squad as transferred.

    `classic_per_game` is the player's form at his OLD club last season. It is what he
    did, not a projection of what he will do at the new one; pair it with get_role_outlook
    to see the minutes he is walking into. Players marked `new_to_competition` have no
    history in this warehouse at all, so no number is offered for them rather than a
    fabricated one.

    Args:
        season: the upcoming season, e.g. "E2026".
        previous_season: season to compare against, e.g. "E2025".
        team_code: restrict to one club (arriving or leaving).
        status: "moved", "stayed", "new_to_competition", "unsigned" or "all".
        min_classic: only players who averaged at least this many classic points.
        limit: max rows.
    """
    have = _query(
        "SELECT count(*) AS n FROM announced_rosters WHERE season_code = ?", [season]
    )
    rows = have.get("rows") or []
    if not rows or not rows[0].get("n"):
        return {
            "error": f"No announced rosters loaded for {season}.",
            "how_to_fix": (
                f"Run: uv run python -m euroleague_open_data.rosters --season {season}. "
                "Report this as 'rosters not loaded', which is different from 'no player "
                "changed club'."
            ),
        }

    if status == "unsigned":
        out = _query(
            """SELECT u.*, o.status AS known_status, o.note AS known_note
               FROM (""" + rosters.UNSIGNED_SQL + """) u
               LEFT JOIN player_overrides o ON o.player_name = u.player_name
               ORDER BY u.classic_per_game DESC NULLS LAST LIMIT ?""",
            [previous_season, previous_season, season, limit],
        )
        out["meaning"] = (
            f"Played in {previous_season} and appears on no announced {season} roster. "
            "Upstream cannot say why: a player who signed in the NBA looks exactly like "
            "one still negotiating. `known_status` is filled in only where a human "
            "recorded the reason in data/overrides.csv; where it is null, absence means "
            "nothing more than absence."
        )
        return out

    # Filter over the finished projection rather than inside it, so the column names here
    # are the ones the caller sees.
    params: list[Any] = [season, previous_season, previous_season]
    where: list[str] = []
    if status != "all":
        where.append("status = ?")
        params.append(status)
    if team_code:
        where.append("(to_team_code = ? OR from_team_code = ?)")
        params.extend([team_code, team_code])
    if min_classic:
        where.append("coalesce(classic_per_game, 0) >= ?")
        params.append(min_classic)

    sql = f"SELECT * FROM ({rosters.TRANSFERS_SQL})"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY classic_per_game DESC NULLS LAST LIMIT ?"
    params.append(limit)

    result = _query(sql, params)
    result["source"] = "clubs' announced squads, not games played"
    result["matched_on"] = "team_code, so sponsor renames are not counted as transfers"
    return result


@mcp.tool()
def get_squad_outlook(
    team_code: str,
    season: str = "E2026",
    previous_season: str = "E2025",
) -> dict[str, Any]:
    """A club's announced squad for the coming season: who left, who arrived, what is open.

    Use this, not get_role_outlook, for anything about the season ahead. get_role_outlook
    compares two seasons that were both played, so before a ball is thrown up it describes
    LAST summer's departures -- a whole transfer window out of date.

    Read the minute budget, not the names. Each position has roughly the same minutes to
    give as last season, so when the arrivals' previous workloads add up to more than the
    club has, somebody's minutes fall. That is the mechanism behind a good player scoring
    less at a new club: among players who changed club last summer, the ones who dropped
    were the ones who lost minutes, not simply the ones who moved.

    `status` is the important column:
      returning               - was at this club last season
      arrived                 - came from another club in this competition, numbers apply
                                to his OLD role, not the new one
      no_history_in_warehouse - has a person code but no rows here. Valanciunas returning
                                to Zalgiris from the NBA is this: he played in 2011-13,
                                which is not loaded.
      new_to_competition      - no code at all, typically an NBA or domestic-league arrival

    For the last two, this warehouse can tell you the ROLE and nothing about the PLAYER.
    State the opening he walks into -- minutes and production the club lost at his position,
    who he competes with -- and say plainly that his own level is an input you do not have.
    Do not invent a projection, and never let a missing number read as zero.

    Args:
        team_code: club code from search_teams.
        season: the upcoming season, e.g. "E2026".
        previous_season: the last played season, e.g. "E2025".
    """
    loaded = _query(
        "SELECT count(*) AS n FROM announced_rosters WHERE season_code = ? AND team_code = ?",
        [season, team_code],
    )
    rows = loaded.get("rows") or []
    if not rows or not rows[0].get("n"):
        return {
            "error": f"No announced roster for {team_code} in {season}.",
            "how_to_fix": (
                f"Run: uv run python -m euroleague_open_data.rosters --season {season}. "
                "This is 'not loaded', not 'the club has no players'."
            ),
        }

    gone = _query(
        rosters.DEPARTURES_SQL,
        [previous_season, previous_season, previous_season, team_code, season, team_code],
    )
    squad = _query(
        rosters.SQUAD_SQL,
        [previous_season, previous_season, previous_season, season, team_code],
    )
    coach = _query(
        """SELECT coach_name, games_coached, avg_players_15plus, rotation_style
           FROM coach_rotation_profile WHERE team_code = ? AND season_code = ?
           ORDER BY games_coached DESC""",
        [team_code, previous_season],
    )

    # A failed query must not arrive as an empty section. `_query` returns {"error": ...}
    # rather than raising, so reading .get("rows") straight through turns a broken SQL
    # statement into "this club lost nobody" -- which is exactly how a missing alias in
    # DEPARTURES_SQL once reported Zalgiris as having kept Francisco and Wright.
    for name, result in (("departures", gone), ("squad", squad)):
        if "error" in result:
            return {
                "error": f"the {name} query failed: {result['error']}",
                "note": "This is a failure, not an empty result. Do not report it as no changes.",
            }

    departed = gone.get("rows") or []
    members = squad.get("rows") or []

    # The minute budget, per position. Last season's total is the size of the pot; the
    # arrivals' previous minutes are what they are used to being paid out of it.
    budget: dict[str, dict[str, float]] = {}
    for row in departed:
        pos = row.get("position") or "Unknown"
        b = budget.setdefault(pos, {"freed_minutes_per_game": 0.0, "claimed_by_squad": 0.0})
        b["freed_minutes_per_game"] += row.get("minutes_per_game") or 0.0
    for row in members:
        pos = row.get("position") or "Unknown"
        b = budget.setdefault(pos, {"freed_minutes_per_game": 0.0, "claimed_by_squad": 0.0})
        b["claimed_by_squad"] += row.get("minutes_per_game") or 0.0
    for b in budget.values():
        b["freed_minutes_per_game"] = round(b["freed_minutes_per_game"], 1)
        b["claimed_by_squad"] = round(b["claimed_by_squad"], 1)

    unknown = [r for r in members if r.get("status") in
               ("no_history_in_warehouse", "new_to_competition")]

    return {
        "departed": departed,
        "squad": members,
        "minute_budget_by_position": budget,
        "coaching_last_season": coach.get("rows"),
        "players_without_history": [r["player_name"] for r in unknown],
        "how_to_read_the_budget": (
            "claimed_by_squad sums what each announced player played LAST season, at his "
            "old club. A position where that total exceeds roughly the minutes the club "
            "actually has (about 200 across the five spots) is oversubscribed: those "
            "players cannot all keep their previous workload."
        ),
        "caveat": (
            f"{len(unknown)} players in this squad have no rows in this warehouse. No "
            "projection is possible for them from this data -- describe the role they are "
            "walking into and say their own level is unknown here."
        ),
    }


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
