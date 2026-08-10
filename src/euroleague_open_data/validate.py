"""Reconciliation checks over the built warehouse.

The purpose is to fail loudly. Silently ingesting wrong numbers is worse than shipping
nothing, because a plausible wrong number gets quoted by an LLM as fact.

Every check returns a row count of violations plus enough detail to find them. The report
is written to disk and committed, so regressions appear in git history rather than being
discovered by a user.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

log = logging.getLogger(__name__)


@dataclass
class Check:
    name: str
    description: str
    severity: str  # "error" blocks publication, "warning" is recorded only
    violations: int = 0
    checked: int = 0
    examples: list[Any] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.violations == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "severity": self.severity,
            "passed": self.passed,
            "checked": self.checked,
            "violations": self.violations,
            "examples": self.examples[:5],
        }


def _run(con: duckdb.DuckDBPyConnection, check: Check, total_sql: str, bad_sql: str) -> Check:
    total = con.execute(total_sql).fetchone()
    check.checked = int(total[0]) if total else 0
    rows = con.execute(bad_sql).fetchall()
    check.violations = len(rows)
    check.examples = [list(r) for r in rows[:5]]
    return check


def run_all(db_path: Path) -> dict[str, Any]:
    con = duckdb.connect(str(db_path), read_only=True)
    checks: list[Check] = []

    # 1. Team boxscore totals must equal the final score recorded on the game.
    checks.append(
        _run(
            con,
            Check(
                "team_total_vs_final_score",
                "boxscores_team.points equals games.home_score/away_score",
                "error",
            ),
            "SELECT count(*) FROM boxscores_team",
            """
            SELECT b.game_code, b.is_home, b.points,
                   CASE WHEN b.is_home THEN g.home_score ELSE g.away_score END
            FROM boxscores_team b
            JOIN games g USING (season_code, game_code)
            WHERE b.points IS DISTINCT FROM
                  CASE WHEN b.is_home THEN g.home_score ELSE g.away_score END
            """,
        )
    )

    # 2. Summed player points must equal the team total for that side.
    checks.append(
        _run(
            con,
            Check(
                "player_sum_vs_team_total",
                "sum(boxscores_player.points) equals boxscores_team.points",
                "error",
            ),
            "SELECT count(*) FROM boxscores_team",
            """
            SELECT t.season_code, t.game_code, t.is_home, t.points, sum(p.points)
            FROM boxscores_team t
            JOIN boxscores_player p
              ON p.season_code = t.season_code
             AND p.game_code = t.game_code
             AND p.is_home = t.is_home
            GROUP BY t.season_code, t.game_code, t.is_home, t.points
            HAVING t.points IS DISTINCT FROM sum(p.points)
            """,
        )
    )

    # 3. Play-by-play scoring must reconcile against the boxscore.
    checks.append(
        _run(
            con,
            Check(
                "pbp_points_vs_boxscore",
                "points implied by play-by-play scoring events equal the boxscore total",
                "error",
            ),
            "SELECT count(DISTINCT (season_code, game_code)) FROM play_by_play",
            """
            WITH pbp AS (
              SELECT season_code, game_code, team_code,
                     sum(CASE play_type WHEN '2FGM' THEN 2 WHEN '3FGM' THEN 3
                                        WHEN 'FTM' THEN 1 ELSE 0 END) AS pts
              FROM play_by_play
              WHERE team_code IS NOT NULL
              GROUP BY 1, 2, 3
            )
            SELECT pbp.season_code, pbp.game_code, pbp.team_code, pbp.pts, b.points
            FROM pbp
            JOIN boxscores_team b
              ON b.season_code = pbp.season_code
             AND b.game_code = pbp.game_code
             AND b.team_code = pbp.team_code
            WHERE pbp.pts IS DISTINCT FROM b.points
            """,
        )
    )

    # 4. Shot rows must reconcile against boxscore field-goal attempts.
    checks.append(
        _run(
            con,
            Check(
                "shots_vs_boxscore_fga",
                "shot rows per team equal boxscore field goals attempted",
                "warning",
            ),
            "SELECT count(DISTINCT (season_code, game_code)) FROM shots",
            """
            WITH s AS (
              SELECT season_code, game_code, team_code, count(*) AS n
              FROM shots
              WHERE action_id IN ('2FGA','2FGM','3FGA','3FGM')
              GROUP BY 1, 2, 3
            )
            SELECT s.season_code, s.game_code, s.team_code, s.n, b.fga
            FROM s JOIN boxscores_team b
              ON b.season_code = s.season_code
             AND b.game_code = s.game_code
             AND b.team_code = s.team_code
            WHERE s.n IS DISTINCT FROM b.fga
            """,
        )
    )

    # 5. Event ordering. There is no absolute clock upstream (MARKERTIME is frequently
    #    empty), so NUMBEROFPLAY is the only ordering signal available -- and in roughly a
    #    third of games it disagrees with the period bucketing the upstream itself used.
    #
    #    Measured on E2025: game 6 has period 1 spanning play numbers 10-253 while period 2
    #    spans 157-311. The ranges overlap, so the two orderings cannot both be right.
    #
    #    Any game flagged here is unreliable for lineup reconstruction, which depends
    #    entirely on replaying substitutions in true order.
    checks.append(
        _run(
            con,
            Check(
                "pbp_period_ranges_disjoint",
                "play_number ranges of consecutive periods do not overlap",
                "warning",
            ),
            "SELECT count(DISTINCT (season_code, game_code)) FROM play_by_play",
            """
            WITH spans AS (
              SELECT season_code, game_code, period,
                     min(play_number) AS lo, max(play_number) AS hi
              FROM play_by_play
              GROUP BY 1, 2, 3
            ),
            adjacent AS (
              SELECT season_code, game_code, period, lo, hi,
                     lag(hi) OVER (PARTITION BY season_code, game_code ORDER BY period)
                       AS prev_hi
              FROM spans
            )
            SELECT season_code, game_code, period, prev_hi, lo
            FROM adjacent
            WHERE prev_hi IS NOT NULL AND lo <= prev_hi
            """,
        )
    )

    # 5b. Duplicate play numbers inside one period are unambiguous corruption: the
    #     sequence is meant to be a unique ordering key for the whole game.
    checks.append(
        _run(
            con,
            Check(
                "pbp_play_number_unique",
                "play_number is unique within a game",
                "warning",
            ),
            "SELECT count(*) FROM play_by_play",
            """
            SELECT season_code, game_code, play_number, count(*)
            FROM play_by_play
            GROUP BY 1, 2, 3
            HAVING count(*) > 1
            """,
        )
    )

    # 6. Every actor referenced by the live feeds must resolve to a known person.
    checks.append(
        _run(
            con,
            Check(
                "person_crosswalk_resolves",
                "shots and play-by-play person codes resolve to the players table",
                "warning",
            ),
            "SELECT count(*) FROM shots",
            """
            SELECT DISTINCT e.person_code, any_value(e.player_name)
            FROM (
              SELECT person_code, player_name FROM shots
              UNION ALL
              SELECT person_code, player_name FROM play_by_play
            ) e
            LEFT JOIN players p USING (person_code)
            WHERE e.person_code IS NOT NULL
              AND e.person_code NOT IN ('CO_A','CO_B')
              AND p.person_code IS NULL
            GROUP BY e.person_code
            """,
        )
    )

    # 7. A 200 response is not proof of data. U2013 and U2016 return empty shot arrays.
    checks.append(
        _run(
            con,
            Check(
                "played_games_have_detail",
                "every played game has boxscore, shot and play-by-play rows",
                "warning",
            ),
            "SELECT count(*) FROM games WHERE played",
            """
            SELECT g.game_code,
                   (SELECT count(*) FROM boxscores_player b WHERE b.game_code = g.game_code),
                   (SELECT count(*) FROM shots s WHERE s.game_code = g.game_code),
                   (SELECT count(*) FROM play_by_play p WHERE p.game_code = g.game_code)
            FROM games g
            WHERE g.played
              AND (
                (SELECT count(*) FROM boxscores_player b WHERE b.game_code = g.game_code) = 0
                OR (SELECT count(*) FROM shots s WHERE s.game_code = g.game_code) = 0
                OR (SELECT count(*) FROM play_by_play p WHERE p.game_code = g.game_code) = 0
              )
            """,
        )
    )

    row_counts = {
        table: int(con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # type: ignore[index]
        for (table,) in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        ).fetchall()
    }
    con.close()

    errors = [c for c in checks if c.severity == "error" and not c.passed]
    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "database": str(db_path),
        "row_counts": row_counts,
        "checks": [c.as_dict() for c in checks],
        "blocking_failures": len(errors),
        "status": "FAIL" if errors else "PASS",
    }

    for c in checks:
        mark = "PASS" if c.passed else ("FAIL" if c.severity == "error" else "WARN")
        log.info("%-5s %-32s %d/%d violations", mark, c.name, c.violations, c.checked)

    return report


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Validate the warehouse.")
    parser.add_argument("--db", type=Path, default=Path("data/euroleague.duckdb"))
    parser.add_argument("--report", type=Path, default=Path("docs/data-quality-report.json"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    report = run_all(args.db)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2))
    log.info("status: %s -> %s", report["status"], args.report)

    if report["status"] == "FAIL":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
