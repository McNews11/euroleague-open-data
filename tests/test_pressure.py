"""Minute pressure on the draft board.

The adjustment is small and the evidence behind it is weak, so the tests are about it
behaving exactly as documented rather than about the size of the effect: applied above the
threshold, never below it, and never silently absent.
"""

from __future__ import annotations

import pytest

from euroleague_open_data import rosters


def test_params_match_the_placeholders() -> None:
    """The query and its bind values must change together.

    Counting `?` by eye at the call site is what produced "Values were not provided for
    prepared statement parameter 10" on a board that had worked a minute earlier.
    """
    placeholders = rosters.PRESSURE_SQL.count("?")
    supplied = len(rosters.pressure_params("E2025", "E2026"))
    assert placeholders == supplied, (
        f"PRESSURE_SQL has {placeholders} placeholders but pressure_params supplies "
        f"{supplied}. Update both."
    )


def test_bands_are_ordered_and_documented() -> None:
    assert rosters.PRESSURE_LOW < rosters.PRESSURE_HIGH
    assert rosters.PRESSURE_PENALTY > 0
    # The penalty is the measured gap between the outer terciles, not a round number
    # someone liked. If it changes, the backtest in the module docstring must too.
    assert pytest.approx(2.8) == rosters.PRESSURE_PENALTY


@pytest.mark.parametrize(
    "pressure,expect_penalty",
    [(2.17, True), (1.25, True), (1.24, False), (1.11, False), (0.35, False)],
)
def test_penalty_applies_only_at_or_above_the_threshold(
    pressure: float, expect_penalty: bool
) -> None:
    """Mirrors the CASE expression in the tool, which is where the rule actually lives."""
    base = 20.0
    adjusted = base - (rosters.PRESSURE_PENALTY if pressure >= rosters.PRESSURE_HIGH else 0)
    assert (adjusted < base) is expect_penalty


def test_missing_rosters_are_reported_not_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """No rosters loaded is 'unknown', never 'nobody competes for his minutes'."""
    from euroleague_open_data import mcp_server

    monkeypatch.setattr(
        mcp_server, "_query",
        lambda sql, params=None, **kw: (
            {"rows": [{"n": 0}]} if "announced_rosters" in sql else {"rows": [], "row_count": 0}
        ),
    )
    out = mcp_server.get_draft_board(teams=8, limit=3, next_season="E2030")
    assert "not applied" in out["minute_pressure"]
    assert "unknown" in out["minute_pressure"].lower()


def test_pressure_can_be_switched_off_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    """A drafter who distrusts the adjustment must be able to see the raw board."""
    from euroleague_open_data import mcp_server

    seen: list[str] = []

    def fake(sql, params=None, **kw):
        seen.append(sql)
        return {"rows": [{"n": 5}]} if "count(*) AS n" in sql else {"rows": [], "row_count": 0}

    monkeypatch.setattr(mcp_server, "_query", fake)
    mcp_server.get_draft_board(teams=8, limit=3, adjust_for_minutes=False)
    board_sql = seen[-1]
    assert "ORDER BY b.vorp_per_game" in board_sql
    assert "ORDER BY adjusted_per_game" not in board_sql
