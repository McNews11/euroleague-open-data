"""Derived metrics, materialised as tables rather than computed per query.

Materialising matters here for a specific reason: the MCP server answers questions from an
LLM, and an LLM will happily ask for a season-wide leaderboard several times in a row. The
arithmetic should happen once at build time, not once per question.

Formulae follow the standard basketball-reference definitions, with the 0.44 free-throw
coefficient for possession estimation.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)

# Minutes come from upstream as seconds ("timePlayed": 2400.0 for a whole team-game).
PLAYER_SEASON_SQL = """
CREATE OR REPLACE TABLE player_season_stats AS
WITH team_game AS (
    SELECT season_code, game_code, team_code,
           fga AS team_fga, fta AS team_fta, turnovers AS team_tov,
           seconds_played AS team_seconds
    FROM boxscores_team
),
joined AS (
    SELECT p.*, t.team_fga, t.team_fta, t.team_tov, t.team_seconds
    FROM boxscores_player p
    JOIN team_game t
      ON t.season_code = p.season_code
     AND t.game_code   = p.game_code
     AND t.team_code   = p.team_code
),
per_player AS (
    SELECT
        season_code,
        person_code,
        any_value(team_code)                          AS team_code,
        count(*) FILTER (WHERE seconds_played > 0)    AS games_played,
        count(*)                                      AS games_on_roster,
        sum(seconds_played) / 60.0                    AS minutes,
        sum(points)                                   AS points,
        sum(fg2m) AS fg2m, sum(fg2a) AS fg2a,
        sum(fg3m) AS fg3m, sum(fg3a) AS fg3a,
        sum(ftm)  AS ftm,  sum(fta)  AS fta,
        sum(fgm)  AS fgm,  sum(fga)  AS fga,
        sum(rebounds_offensive) AS rebounds_offensive,
        sum(rebounds_defensive) AS rebounds_defensive,
        sum(rebounds_total)     AS rebounds_total,
        sum(assists) AS assists, sum(steals) AS steals,
        sum(turnovers) AS turnovers,
        sum(blocks_favour) AS blocks, sum(blocks_against) AS blocks_against,
        sum(fouls_committed) AS fouls_committed,
        sum(fouls_received)  AS fouls_received,
        sum(valuation)  AS valuation,
        sum(plus_minus) AS plus_minus,
        sum(team_fga)     AS team_fga,
        sum(team_fta)     AS team_fta,
        sum(team_tov)     AS team_tov,
        sum(team_seconds) AS team_seconds
    FROM joined
    GROUP BY season_code, person_code
)
SELECT
    s.season_code,
    s.person_code,
    pl.name        AS player_name,
    s.team_code,
    s.games_played,
    s.games_on_roster,
    round(s.minutes, 1) AS minutes,
    s.points, s.fg2m, s.fg2a, s.fg3m, s.fg3a, s.ftm, s.fta, s.fgm, s.fga,
    s.rebounds_offensive, s.rebounds_defensive, s.rebounds_total,
    s.assists, s.steals, s.turnovers, s.blocks, s.blocks_against,
    s.fouls_committed, s.fouls_received, s.valuation, s.plus_minus,

    -- per game
    round(s.points  / nullif(s.games_played, 0), 2) AS points_per_game,
    round(s.rebounds_total / nullif(s.games_played, 0), 2) AS rebounds_per_game,
    round(s.assists / nullif(s.games_played, 0), 2) AS assists_per_game,
    round(s.minutes / nullif(s.games_played, 0), 2) AS minutes_per_game,

    -- shooting efficiency
    round(s.points / nullif(2 * (s.fga + 0.44 * s.fta), 0), 4) AS true_shooting_pct,
    round((s.fgm + 0.5 * s.fg3m) / nullif(s.fga, 0), 4)        AS efg_pct,
    round(s.fgm / nullif(s.fga, 0), 4)                          AS fg_pct,
    round(s.fg3m / nullif(s.fg3a, 0), 4)                        AS fg3_pct,
    round(s.ftm / nullif(s.fta, 0), 4)                          AS ft_pct,

    -- Usage: share of team possessions a player ends while on the floor.
    --
    --   USG% = 100 * (FGA + 0.44*FTA + TOV) * (Tm MP / 5) / (MP * (Tm FGA + 0.44*Tm FTA + Tm TOV))
    --
    -- Upstream stores boxscores_team.seconds_played as the GAME length (2400s = 40 min),
    -- not as the sum of the five players' time on court (which is 12000s). So `Tm MP / 5`
    -- is team_seconds/60 directly -- dividing by 5 again understates usage 5x.
    round(
        100.0 * ((s.fga + 0.44 * s.fta + s.turnovers) * (s.team_seconds / 60.0))
        / nullif(s.minutes * (s.team_fga + 0.44 * s.team_fta + s.team_tov), 0),
        2
    ) AS usage_pct,

    -- per 36 minutes
    round(36.0 * s.points  / nullif(s.minutes, 0), 2) AS points_per_36,
    round(36.0 * s.rebounds_total / nullif(s.minutes, 0), 2) AS rebounds_per_36,
    round(36.0 * s.assists / nullif(s.minutes, 0), 2) AS assists_per_36
FROM per_player s
LEFT JOIN players pl USING (person_code)
"""

TEAM_SEASON_SQL = """
CREATE OR REPLACE TABLE team_season_stats AS
WITH opponent AS (
    SELECT a.season_code, a.game_code, a.team_code,
           b.points AS opp_points, b.fga AS opp_fga, b.fta AS opp_fta,
           b.turnovers AS opp_tov, b.fgm AS opp_fgm, b.fg3m AS opp_fg3m,
           b.rebounds_offensive AS opp_oreb, b.rebounds_defensive AS opp_dreb
    FROM boxscores_team a
    JOIN boxscores_team b
      ON b.season_code = a.season_code
     AND b.game_code   = a.game_code
     AND b.is_home    <> a.is_home
),
agg AS (
    SELECT
        t.season_code, t.team_code,
        count(*) AS games,
        sum(t.points) AS points, sum(o.opp_points) AS opp_points,
        sum(t.fgm) AS fgm, sum(t.fga) AS fga, sum(t.fg3m) AS fg3m,
        sum(t.ftm) AS ftm, sum(t.fta) AS fta,
        sum(t.turnovers) AS turnovers,
        sum(t.rebounds_offensive) AS oreb, sum(t.rebounds_defensive) AS dreb,
        sum(o.opp_fga) AS opp_fga, sum(o.opp_fta) AS opp_fta,
        sum(o.opp_tov) AS opp_tov, sum(o.opp_fgm) AS opp_fgm,
        sum(o.opp_fg3m) AS opp_fg3m,
        sum(o.opp_oreb) AS opp_oreb, sum(o.opp_dreb) AS opp_dreb
    FROM boxscores_team t
    JOIN opponent o
      ON o.season_code = t.season_code
     AND o.game_code   = t.game_code
     AND o.team_code   = t.team_code
    GROUP BY 1, 2
)
SELECT
    a.season_code, a.team_code, tm.name AS team_name, a.games,
    a.points, a.opp_points,
    round(a.points / nullif(a.games, 0), 2)     AS points_per_game,
    round(a.opp_points / nullif(a.games, 0), 2) AS opp_points_per_game,

    -- possession estimate, averaged over both teams' versions
    round(
        0.5 * ((a.fga + 0.44 * a.fta - a.oreb + a.turnovers)
             + (a.opp_fga + 0.44 * a.opp_fta - a.opp_oreb + a.opp_tov)), 1
    ) AS possessions,

    round(100.0 * a.points / nullif(
        0.5 * ((a.fga + 0.44 * a.fta - a.oreb + a.turnovers)
             + (a.opp_fga + 0.44 * a.opp_fta - a.opp_oreb + a.opp_tov)), 0), 2
    ) AS offensive_rating,
    round(100.0 * a.opp_points / nullif(
        0.5 * ((a.fga + 0.44 * a.fta - a.oreb + a.turnovers)
             + (a.opp_fga + 0.44 * a.opp_fta - a.opp_oreb + a.opp_tov)), 0), 2
    ) AS defensive_rating,

    -- Four Factors, own and opponent
    round((a.fgm + 0.5 * a.fg3m) / nullif(a.fga, 0), 4) AS efg_pct,
    round(a.turnovers / nullif(a.fga + 0.44 * a.fta + a.turnovers, 0), 4) AS tov_pct,
    round(a.oreb / nullif(a.oreb + a.opp_dreb, 0), 4) AS oreb_pct,
    round(a.fta / nullif(a.fga, 0), 4) AS ft_rate,
    round((a.opp_fgm + 0.5 * a.opp_fg3m) / nullif(a.opp_fga, 0), 4) AS opp_efg_pct,
    round(a.opp_tov / nullif(a.opp_fga + 0.44 * a.opp_fta + a.opp_tov, 0), 4) AS opp_tov_pct,
    round(a.opp_oreb / nullif(a.opp_oreb + a.dreb, 0), 4) AS opp_oreb_pct,
    round(a.opp_fta / nullif(a.opp_fga, 0), 4) AS opp_ft_rate
FROM agg a
LEFT JOIN (SELECT DISTINCT season_code, team_code, name FROM teams) tm
       ON tm.season_code = a.season_code AND tm.team_code = a.team_code
"""

SHOT_ZONE_SQL = """
CREATE OR REPLACE TABLE shot_zones AS
SELECT
    season_code,
    person_code,
    team_code,
    zone,
    CASE zone
        WHEN 'A' THEN 'Restricted area'
        WHEN 'B' THEN 'Paint (non-RA)'
        WHEN 'C' THEN 'Mid-range baseline'
        WHEN 'D' THEN 'Mid-range wing'
        WHEN 'E' THEN 'Mid-range top'
        WHEN 'F' THEN 'Corner 3'
        WHEN 'G' THEN 'Wing 3'
        WHEN 'H' THEN 'Above-break 3'
        WHEN 'I' THEN 'Deep 3'
        WHEN 'J' THEN 'Backcourt'
        ELSE 'Unknown'
    END AS zone_name,
    count(*)                                   AS attempts,
    count(*) FILTER (WHERE points > 0)         AS makes,
    round(count(*) FILTER (WHERE points > 0) * 1.0 / nullif(count(*), 0), 4) AS fg_pct,
    sum(points)                                AS points,
    round(avg(coord_x), 1)                     AS avg_x,
    round(avg(coord_y), 1)                     AS avg_y
FROM shots
WHERE action_id IN ('2FGA','2FGM','3FGA','3FGM')
GROUP BY season_code, person_code, team_code, zone
"""

# Zone letters are upstream's own coding. The mapping above is inferred from coordinate
# centroids and is recorded as provisional -- see docs/DATA_QUALITY.md.

COMPLETENESS_SQL = """
CREATE OR REPLACE TABLE game_completeness AS
SELECT
    g.season_code,
    g.game_code,
    g.played,
    (SELECT count(*) FROM boxscores_player b
      WHERE b.season_code = g.season_code AND b.game_code = g.game_code) > 0 AS has_boxscore,
    (SELECT count(*) FROM shots s
      WHERE s.season_code = g.season_code AND s.game_code = g.game_code) > 0 AS has_shots,
    (SELECT count(*) FROM play_by_play p
      WHERE p.season_code = g.season_code AND p.game_code = g.game_code) > 0 AS has_play_by_play,
    NOT EXISTS (
        WITH spans AS (
            SELECT period, min(play_number) lo, max(play_number) hi
            FROM play_by_play p
            WHERE p.season_code = g.season_code AND p.game_code = g.game_code
            GROUP BY period
        ), adj AS (
            SELECT lo, lag(hi) OVER (ORDER BY period) prev_hi FROM spans
        )
        SELECT 1 FROM adj WHERE prev_hi IS NOT NULL AND lo <= prev_hi
    ) AS lineup_safe
FROM games g
"""


def build(db_path: Path) -> None:
    con = duckdb.connect(str(db_path))
    for name, sql in [
        ("player_season_stats", PLAYER_SEASON_SQL),
        ("team_season_stats", TEAM_SEASON_SQL),
        ("shot_zones", SHOT_ZONE_SQL),
        ("game_completeness", COMPLETENESS_SQL),
    ]:
        con.execute(sql)
        count = con.execute(f"SELECT count(*) FROM {name}").fetchone()
        log.info("%-22s %s rows", name, count[0] if count else "?")
    con.close()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Build derived analytics tables.")
    parser.add_argument("--db", type=Path, default=Path("data/euroleague.duckdb"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    build(args.db)


if __name__ == "__main__":
    main()
