"""BasketNews Fantasy scoring and draft valuation.

Rules read from https://fantasy.basketnews.com/lt/rules on 2026-08-10 (draft mode tab).

Every term in the modern system maps onto a field the warehouse already stores, so these
are not estimates -- they are the exact points a player scored, recomputed from the
boxscore. That matters for drafting: rankings built on real scoring beat rankings built
on PIR, because the two systems disagree sharply about volume shooters.

ASSUMPTIONS, flagged because the rules page does not settle them:

1. "Classic" scoring is described only as "traditional scoring based on player statistics".
   The budget-mode page gives an explicit formula -- PIR +/-10% by team result -- so that
   is what CLASSIC implements. Confirm with your league commissioner.
2. Double/triple/quadruple-double bonuses are awarded at the highest tier reached only,
   not cumulatively. Flip STACK_DOUBLE_BONUSES to change it.
3. A player who did not play scores zero, including no win/loss bonus. Awarding +1.5 to a
   DNP would make bench-warmers on good teams look draftable, which is plainly wrong.
4. Double-double categories are points, total rebounds, assists, steals, blocks.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)

STACK_DOUBLE_BONUSES = False

# Modern system, points per unit. Source: fantasy.basketnews.com/lt/rules, draft mode.
MODERN_WEIGHTS = {
    "points": 1.0,
    "rebounds_defensive": 1.0,
    "rebounds_offensive": 1.5,
    "assists": 1.5,
    "steals": 1.5,
    "blocks_favour": 1.0,
    "fouls_received": 1.0,
    "missed_fg": -1.0,
    "missed_ft": -1.0,
    "turnovers": -1.5,
    "blocks_against": -0.5,
}
WIN_BONUS = 1.5
LOSS_PENALTY = -1.5
FIVE_FOUL_PENALTY = -5.0
DOUBLE_DOUBLE = 10.0
TRIPLE_DOUBLE = 30.0
QUADRUPLE_DOUBLE = 100.0

CLASSIC_WIN_MULTIPLIER = 1.1
CLASSIC_LOSS_MULTIPLIER = 0.9


FANTASY_GAME_SQL = f"""
CREATE OR REPLACE TABLE fantasy_points_game AS
WITH base AS (
    SELECT
        b.season_code,
        b.game_code,
        b.person_code,
        b.team_code,
        b.is_home,
        b.seconds_played / 60.0                       AS minutes,
        b.points, b.assists, b.steals,
        b.rebounds_total, b.rebounds_offensive, b.rebounds_defensive,
        b.blocks_favour, b.blocks_against,
        b.turnovers, b.fouls_committed, b.fouls_received,
        b.valuation                                   AS pir,
        b.fga - b.fgm                                 AS missed_fg,
        b.fta - b.ftm                                 AS missed_ft,
        CASE WHEN b.is_home THEN g.home_score > g.away_score
             ELSE g.away_score > g.home_score END     AS team_won,
        g.utc_date,
        g.round
    FROM boxscores_player b
    JOIN games g
      ON g.season_code = b.season_code AND g.game_code = b.game_code
),
tiers AS (
    SELECT *,
        (CASE WHEN points          >= 10 THEN 1 ELSE 0 END
       + CASE WHEN rebounds_total  >= 10 THEN 1 ELSE 0 END
       + CASE WHEN assists         >= 10 THEN 1 ELSE 0 END
       + CASE WHEN steals          >= 10 THEN 1 ELSE 0 END
       + CASE WHEN blocks_favour   >= 10 THEN 1 ELSE 0 END) AS double_count
    FROM base
),
scored AS (
    SELECT *,
        -- Positive and negative counting terms.
          {MODERN_WEIGHTS["points"]}              * points
        + {MODERN_WEIGHTS["rebounds_defensive"]}  * rebounds_defensive
        + {MODERN_WEIGHTS["rebounds_offensive"]}  * rebounds_offensive
        + {MODERN_WEIGHTS["assists"]}             * assists
        + {MODERN_WEIGHTS["steals"]}              * steals
        + {MODERN_WEIGHTS["blocks_favour"]}       * blocks_favour
        + {MODERN_WEIGHTS["fouls_received"]}      * fouls_received
        + {MODERN_WEIGHTS["missed_fg"]}           * missed_fg
        + {MODERN_WEIGHTS["missed_ft"]}           * missed_ft
        + {MODERN_WEIGHTS["turnovers"]}           * turnovers
        + {MODERN_WEIGHTS["blocks_against"]}      * blocks_against
        -- Milestone bonus, highest tier only.
        + CASE
            WHEN double_count >= 4 THEN {QUADRUPLE_DOUBLE}
            WHEN double_count = 3 THEN {TRIPLE_DOUBLE}
            WHEN double_count = 2 THEN {DOUBLE_DOUBLE}
            ELSE 0 END
        -- Team result.
        + CASE WHEN team_won THEN {WIN_BONUS} ELSE {LOSS_PENALTY} END
        -- Foul-out penalty.
        + CASE WHEN fouls_committed >= 5 THEN {FIVE_FOUL_PENALTY} ELSE 0 END
        AS modern_raw
    FROM tiers
)
SELECT
    season_code, game_code, person_code, team_code, is_home, round, utc_date,
    round(minutes, 1) AS minutes,
    points, rebounds_total, assists, steals, blocks_favour, turnovers,
    fouls_committed, fouls_received, missed_fg, missed_ft, pir,
    double_count,
    -- A player who did not appear scores nothing at all.
    CASE WHEN minutes > 0 THEN round(modern_raw, 2) ELSE 0 END AS fantasy_modern,
    CASE WHEN minutes > 0 THEN
        round(pir * CASE WHEN team_won THEN {CLASSIC_WIN_MULTIPLIER}
                         ELSE {CLASSIC_LOSS_MULTIPLIER} END, 2)
        ELSE 0 END AS fantasy_classic,
    team_won
FROM scored
"""


FANTASY_SEASON_SQL = """
CREATE OR REPLACE TABLE fantasy_player_season AS
WITH played AS (
    SELECT * FROM fantasy_points_game WHERE minutes > 0
),
agg AS (
    SELECT
        season_code,
        person_code,
        any_value(team_code)                         AS team_code,
        count(*)                                     AS games_played,
        round(sum(minutes), 1)                       AS minutes,
        round(avg(minutes), 1)                       AS minutes_per_game,
        round(sum(fantasy_modern), 1)                AS modern_total,
        round(avg(fantasy_modern), 2)                AS modern_per_game,
        round(stddev_samp(fantasy_modern), 2)        AS modern_stddev,
        round(quantile_cont(fantasy_modern, 0.25), 2) AS modern_floor_p25,
        round(quantile_cont(fantasy_modern, 0.75), 2) AS modern_ceiling_p75,
        round(min(fantasy_modern), 2)                AS modern_worst,
        round(max(fantasy_modern), 2)                AS modern_best,
        round(sum(fantasy_classic), 1)               AS classic_total,
        round(avg(fantasy_classic), 2)               AS classic_per_game,
        round(stddev_samp(fantasy_classic), 2)       AS classic_stddev,
        round(avg(fantasy_modern) / nullif(avg(minutes), 0) * 36, 2) AS modern_per_36,
        sum(CASE WHEN double_count >= 2 THEN 1 ELSE 0 END) AS double_doubles
    FROM played
    GROUP BY season_code, person_code
)
SELECT
    a.*,
    p.name          AS player_name,
    p.position_name,
    p.height_cm,
    p.country_name,
    -- Consistency: mean divided by spread. High means dependable week to week, which
    -- matters more in a draft league than in a budget league, because you keep the pick.
    round(a.modern_per_game / nullif(a.modern_stddev, 0), 3) AS consistency_ratio
FROM agg a
LEFT JOIN players p USING (person_code)
"""


def draft_board_select(teams: int, roster_size: int) -> str:
    """Value over replacement, given the league's shape.

    Replacement level is the point where a position runs out of draftable players. With
    `teams` managers each taking `roster_size` players, the draft consumes teams*roster
    players in total; the share taken at each position follows how the league's minute
    distribution actually splits, so it is measured rather than assumed.

    A player's draft value is production above the best player you could still get at that
    position after the draft ends. That is what makes a scarce centre worth more than an
    equally productive guard.
    """
    drafted = teams * roster_size
    return f"""
WITH pool AS (
    SELECT *,
           coalesce(position_name, 'Unknown') AS pos
    FROM fantasy_player_season
    WHERE games_played >= 5
),
position_share AS (
    SELECT pos, count(*) * 1.0 / sum(count(*)) OVER () AS share
    FROM (SELECT pos FROM pool ORDER BY modern_per_game DESC LIMIT {drafted}) t
    GROUP BY pos
),
ranked AS (
    SELECT p.*,
           row_number() OVER (PARTITION BY p.pos ORDER BY p.modern_per_game DESC) AS pos_rank,
           row_number() OVER (ORDER BY p.modern_per_game DESC) AS overall_rank,
           greatest(1, cast(round({drafted} * s.share) AS INTEGER)) AS pos_slots
    FROM pool p
    LEFT JOIN position_share s ON s.pos = p.pos
),
replacement AS (
    SELECT pos, min(modern_per_game) AS replacement_level
    FROM ranked
    WHERE pos_rank <= pos_slots
    GROUP BY pos
)
SELECT
    r.season_code,
    r.person_code,
    r.overall_rank,
    r.pos_rank,
    r.player_name,
    r.pos            AS position,
    r.team_code,
    r.games_played,
    r.minutes_per_game,
    r.modern_per_game,
    r.modern_total,
    r.modern_stddev,
    r.modern_floor_p25,
    r.modern_ceiling_p75,
    r.consistency_ratio,
    r.double_doubles,
    r.classic_per_game,
    round(rep.replacement_level, 2) AS replacement_level,
    round(r.modern_per_game - rep.replacement_level, 2) AS vorp_per_game,
    round((r.modern_per_game - rep.replacement_level) * r.games_played, 1) AS vorp_total,
    {teams} AS league_teams,
    {roster_size} AS roster_size
FROM ranked r
JOIN replacement rep ON rep.pos = r.pos
ORDER BY vorp_per_game DESC
"""


def build(db_path: Path, *, teams: int = 8, roster_size: int = 13) -> None:
    con = duckdb.connect(str(db_path))
    con.execute(FANTASY_GAME_SQL)
    con.execute(FANTASY_SEASON_SQL)
    con.execute("CREATE OR REPLACE TABLE draft_board AS " + draft_board_select(teams, roster_size))
    for table in ("fantasy_points_game", "fantasy_player_season", "draft_board"):
        count = con.execute(f"SELECT count(*) FROM {table}").fetchone()
        log.info("%-24s %s rows", table, count[0] if count else "?")
    con.close()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Build fantasy scoring and draft board.")
    parser.add_argument("--db", type=Path, default=Path("data/euroleague.duckdb"))
    parser.add_argument("--teams", type=int, default=8, help="managers in the league (3-12)")
    parser.add_argument("--roster-size", type=int, default=13)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    build(args.db, teams=args.teams, roster_size=args.roster_size)


if __name__ == "__main__":
    main()
