"""Players whose only history is EuroCup, or an older EuroLeague season.

Without this they are absent from the draft board entirely -- 28 of them, including one
who averaged 14.02 in the EuroLeague two seasons ago. Absence reads as "not worth a pick",
which is a claim the data does not support.
"""

from __future__ import annotations

import duckdb
import pytest

from euroleague_open_data import rosters


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(":memory:")
    c.execute("""CREATE TABLE announced_rosters (
        season_code VARCHAR, team_code VARCHAR, team_name VARCHAR, person_code VARCHAR,
        player_name VARCHAR, position VARCHAR, dorsal VARCHAR, contract_until VARCHAR,
        has_person_code BOOLEAN)""")
    c.execute("""CREATE TABLE fantasy_points_game (
        season_code VARCHAR, person_code VARCHAR, game_code INTEGER, fantasy_classic DOUBLE)""")
    c.execute("""CREATE TABLE player_overrides (
        person_code VARCHAR, player_name VARCHAR, status VARCHAR, note VARCHAR)""")
    return c


def roster(con, code: str, name: str, pos: str = "Center") -> None:
    con.execute(
        "INSERT INTO announced_rosters VALUES ('E2026','ZAL','Zalgiris Kaunas',?,?,?,'1','',true)",
        [code, name, pos],
    )


def games(con, code: str, season: str, value: float, n: int = 20) -> None:
    for i in range(n):
        con.execute("INSERT INTO fantasy_points_game VALUES (?, ?, ?, ?)", [season, code, i, value])


def fetch(con):
    return con.execute(rosters.EUROCUP_FALLBACK_SQL, ["E2026", "E2025"]).fetchall()


def test_eurocup_production_is_converted_not_taken_at_face_value(con) -> None:
    roster(con, "001", "GACH, BOTH", "Forward")
    games(con, "001", "U2025", 15.36)
    row = fetch(con)[0]
    assert row[5] == pytest.approx(15.36 * rosters.EUROCUP_FACTOR, abs=0.01)
    assert "0.57" in row[9], "the conversion must be visible in the label"


def test_real_euroleague_history_beats_a_converted_estimate(con) -> None:
    """Zizic averaged 22.19 in the EuroCup and 8.85 in the EuroLeague.

    Scaling the EuroCup figure gives 12.65 and ranks him 40 places too high. An actual
    EuroLeague number, even an older one, is evidence; a converted one is an inference.
    """
    roster(con, "002", "ZIZIC, ANTE")
    games(con, "002", "U2025", 22.19)
    games(con, "002", "E2024", 8.85)
    rows = fetch(con)
    assert len(rows) == 1, "one row per player, not one per season"
    assert rows[0][5] == pytest.approx(8.85, abs=0.01)
    assert rows[0][9] == "E2024"


def test_the_most_recent_euroleague_season_wins(con) -> None:
    roster(con, "003", "SOMEONE, ELSE")
    games(con, "003", "E2023", 5.0)
    games(con, "003", "E2024", 11.0)
    assert fetch(con)[0][5] == pytest.approx(11.0, abs=0.01)


def test_players_with_current_history_are_left_to_the_main_board(con) -> None:
    """They are already ranked properly; adding them here would duplicate the row."""
    roster(con, "004", "VEZENKOV, SASHA")
    games(con, "004", "E2025", 23.3)
    assert fetch(con) == []


def test_a_short_sample_is_not_promoted(con) -> None:
    roster(con, "005", "CAMEO, PLAYER")
    games(con, "005", "U2025", 30.0, n=rosters.EUROCUP_MIN_GAMES - 1)
    assert fetch(con) == [], "a handful of games is not a season"


def test_overridden_players_stay_out(con) -> None:
    """Someone recorded as gone must not reappear through the back door."""
    roster(con, "006", "DIALLO, ALPHA", "Forward")
    games(con, "006", "U2025", 20.0)
    con.execute("INSERT INTO player_overrides VALUES ('006','DIALLO, ALPHA','left_league','NBA')")
    assert fetch(con) == []
