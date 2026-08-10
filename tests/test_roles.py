"""Vacated-role logic.

Both tests here exist because of bugs that shipped and were caught in review, not because
of hypothetical risks. Both had the same shape: the code reported an absence of data as a
fact about the world, and did it silently.
"""

import duckdb
import pytest

from euroleague_open_data import roles


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "roles.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE seasons (season_code VARCHAR, competition_code VARCHAR, year INTEGER)
    """)
    con.execute("""
        INSERT INTO seasons VALUES ('E2024','E',2024), ('E2025','E',2025), ('E2026','E',2026)
    """)
    con.execute("""
        CREATE TABLE player_role_stability (
            season_code VARCHAR, person_code VARCHAR, team_code VARCHAR,
            position_name VARCHAR, minutes_per_game DOUBLE, games_played BIGINT
        )
    """)
    con.execute("""
        CREATE TABLE fantasy_player_season (
            season_code VARCHAR, person_code VARCHAR, modern_per_game DOUBLE
        )
    """)
    con.execute("""
        CREATE TABLE player_team_spells (
            season_code VARCHAR, person_code VARCHAR, team_code VARCHAR
        )
    """)
    con.execute("""
        CREATE TABLE boxscores_player (
            season_code VARCHAR, person_code VARCHAR, team_code VARCHAR
        )
    """)
    yield path, con
    con.close()


def _vacated(path):
    # Exercises the vacated-role SQL directly rather than through build(), which would
    # regenerate player_role_stability from boxscores and discard the fixture's rows.
    con = duckdb.connect(str(path))
    con.execute(roles.VACATED_ROLE_SQL)
    rows = con.execute(
        "SELECT season_code, team_code, position_name, departed_players, "
        "vacated_minutes_per_game FROM vacated_role ORDER BY season_code"
    ).fetchall()
    con.close()
    return rows


def test_departure_is_detected_between_consecutive_seasons(db):
    path, con = db
    # Two players produced for ZAL in E2024. Only one is on the E2025 roster.
    con.execute("""
        INSERT INTO player_role_stability VALUES
            ('E2024','stay','ZAL','Guard', 25.0, 30),
            ('E2024','gone','ZAL','Center', 18.0, 30)
    """)
    con.execute("INSERT INTO boxscores_player VALUES ('E2025','stay','ZAL')")
    con.close()

    rows = _vacated(path)
    assert len(rows) == 1
    season, team, position, departed, minutes = rows[0]
    assert (season, team, position) == ("E2025", "ZAL", "Center")
    assert departed == 1
    assert minutes == pytest.approx(18.0)


def test_season_without_roster_data_reports_nothing(db):
    """The bug: with only E2025 loaded, E2026 has an empty roster, so every player in the
    warehouse read as having departed. It claimed 340 departures from a season that had
    simply not been crawled."""
    path, con = db
    con.execute("INSERT INTO player_role_stability VALUES ('E2025','a','ZAL','Guard',25.0,30)")
    con.execute("INSERT INTO boxscores_player VALUES ('E2025','a','ZAL')")
    con.close()

    rows = _vacated(path)
    assert [r for r in rows if r[0] == "E2026"] == [], (
        "a season with no roster data must produce no departures, not a full roster of them"
    )


def test_single_loaded_season_produces_empty_table(db):
    """With one season there is nothing to compare against, so the answer is 'unknown',
    expressed as no rows rather than as zeroes."""
    path, con = db
    con.execute("INSERT INTO player_role_stability VALUES ('E2025','a','ZAL','Guard',25.0,30)")
    con.execute("INSERT INTO boxscores_player VALUES ('E2025','a','ZAL')")
    con.close()

    assert _vacated(path) == []
