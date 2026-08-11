"""Transfer detection for a season that has not been played.

Both bugs covered here produced confident, plausible-looking output: a rebranded club read
as a squad-wide transfer, and a mid-season signing read as two separate moves from two
different clubs. Nothing errored in either case, which is why they need tests.
"""

from __future__ import annotations

import duckdb
import pytest

from euroleague_open_data.rosters import TRANSFERS_SQL, UNSIGNED_SQL


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(":memory:")
    c.execute("""
        CREATE TABLE announced_rosters (
            season_code VARCHAR, team_code VARCHAR, team_name VARCHAR,
            person_code VARCHAR, player_name VARCHAR, position VARCHAR,
            dorsal VARCHAR, contract_until VARCHAR, has_person_code BOOLEAN)
    """)
    c.execute("""
        CREATE TABLE player_team_spells (
            season_code VARCHAR, person_code VARCHAR, player_name VARCHAR,
            team_code VARCHAR, team_name VARCHAR, start_date VARCHAR)
    """)
    c.execute("CREATE TABLE teams (season_code VARCHAR, team_code VARCHAR, name VARCHAR)")
    c.execute("CREATE TABLE players (person_code VARCHAR, name VARCHAR)")
    c.execute("""
        CREATE TABLE fantasy_points_game (
            season_code VARCHAR, person_code VARCHAR, game_code INTEGER,
            fantasy_classic DOUBLE)
    """)
    return c


def transfers(con):
    return con.execute(TRANSFERS_SQL, ["E2026", "E2025", "E2025"]).fetchall()


def test_sponsor_rename_is_not_a_transfer(con) -> None:
    """Maccabi Playtika became Maccabi Rapyd. Nobody moved."""
    con.execute("INSERT INTO teams VALUES ('E2025', 'TEL', 'Maccabi Playtika Tel Aviv')")
    con.execute("""INSERT INTO player_team_spells VALUES
        ('E2025', '001', 'HOARD, JAYLEN', 'TEL', 'Maccabi Playtika Tel Aviv', '2025-08-01')""")
    con.execute("""INSERT INTO announced_rosters VALUES
        ('E2026', 'TEL', 'Maccabi Rapyd Tel Aviv', '001', 'HOARD, JAYLEN',
         'Forward', '3', '2027-06-30', true)""")

    rows = transfers(con)
    assert len(rows) == 1
    assert rows[0][-1] == "stayed", "a rebrand must not count as a move"


def test_real_move_is_detected(con) -> None:
    con.execute("INSERT INTO teams VALUES ('E2025', 'ZAL', 'Zalgiris Kaunas')")
    con.execute("""INSERT INTO player_team_spells VALUES
        ('E2025', '002', 'WRIGHT, MOSES', 'ZAL', 'Zalgiris Kaunas', '2025-08-01')""")
    con.execute("""INSERT INTO announced_rosters VALUES
        ('E2026', 'MIL', 'Armani Olimpia Milan', '002', 'WRIGHT, MOSES',
         'Center', '5', '2027-06-30', true)""")

    rows = transfers(con)
    assert len(rows) == 1
    assert rows[0][-1] == "moved"
    assert rows[0][2] == "Zalgiris Kaunas" and rows[0][3] == "Armani Olimpia Milan"


def test_midseason_signing_reports_only_the_last_club(con) -> None:
    """Saben Lee played for Olympiacos, then Efes, then signed for Zalgiris.

    Two spells must not become two transfer rows, and the club he left is the later one.
    """
    con.execute("INSERT INTO teams VALUES ('E2025', 'OLY', 'Olympiacos Piraeus')")
    con.execute("INSERT INTO teams VALUES ('E2025', 'IST', 'Anadolu Efes Istanbul')")
    con.execute("""INSERT INTO player_team_spells VALUES
        ('E2025', '003', 'LEE, SABEN', 'OLY', 'Olympiacos Piraeus', '2025-09-15'),
        ('E2025', '003', 'LEE, SABEN', 'IST', 'Anadolu Efes Istanbul', '2026-01-05')""")
    con.execute("""INSERT INTO announced_rosters VALUES
        ('E2026', 'ZAL', 'Zalgiris Kaunas', '003', 'LEE, SABEN',
         'Guard', '5', '2027-06-30', true)""")

    rows = transfers(con)
    assert len(rows) == 1, "one player, one row"
    assert rows[0][2] == "Anadolu Efes Istanbul", "the club left is the most recent one"


def test_one_row_per_player_when_teams_has_many_seasons(con) -> None:
    """teams holds a row per club per season; joining on code alone duplicates."""
    con.execute("""INSERT INTO teams VALUES
        ('E2024', 'VIR', 'Virtus Segafredo Bologna'),
        ('E2025', 'VIR', 'Virtus Bologna')""")
    con.execute("""INSERT INTO player_team_spells VALUES
        ('E2025', '004', 'EDWARDS, CARSEN', 'VIR', 'Virtus Bologna', '2025-08-01')""")
    con.execute("""INSERT INTO announced_rosters VALUES
        ('E2026', 'ZAL', 'Zalgiris Kaunas', '004', 'EDWARDS, CARSEN',
         'Guard', '4', '2027-06-30', true)""")

    rows = transfers(con)
    assert len(rows) == 1
    assert rows[0][2] == "Virtus Bologna", "use the name from the season being compared"


def test_player_without_history_is_flagged_not_guessed(con) -> None:
    con.execute("""INSERT INTO announced_rosters VALUES
        ('E2026', 'BES', 'Besiktas Istanbul', '005', 'BROWN, VITTO',
         'Forward', '1', '2027-06-30', true)""")
    rows = transfers(con)
    assert rows[0][-1] == "new_to_competition"
    assert rows[0][6] is None, "no fantasy average may be invented for an unknown player"


def test_unsigned_lists_last_season_players_with_no_new_club(con) -> None:
    con.execute("INSERT INTO teams VALUES ('E2025', 'MAD', 'Real Madrid')")
    con.execute("INSERT INTO players VALUES ('006', 'LYLES, TREY')")
    con.execute("""INSERT INTO player_team_spells VALUES
        ('E2025', '006', 'LYLES, TREY', 'MAD', 'Real Madrid', '2025-08-01')""")
    con.execute("INSERT INTO fantasy_points_game VALUES ('E2025', '006', 1, 14.87)")

    rows = con.execute(UNSIGNED_SQL, ["E2025", "E2025", "E2026"]).fetchall()
    assert [r[0] for r in rows] == ["LYLES, TREY"]
    assert rows[0][1] == "Real Madrid"


def _stats_tables(con) -> None:
    """Extra schema the squad views need on top of the shared fixture."""
    con.execute("""CREATE TABLE player_season_stats (
        season_code VARCHAR, person_code VARCHAR, team_code VARCHAR,
        minutes DOUBLE, games_played INTEGER, usage_pct DOUBLE)""")
    con.execute("ALTER TABLE player_team_spells ADD COLUMN position_name VARCHAR")


def test_departures_are_measured_against_the_announced_roster(con) -> None:
    """Not against the previous played season, which is a whole window out of date."""
    from euroleague_open_data.rosters import DEPARTURES_SQL

    _stats_tables(con)
    con.execute("INSERT INTO players VALUES ('001', 'FRANCISCO, SYLVAIN')")
    con.execute("""INSERT INTO player_team_spells VALUES
        ('E2025', '001', 'FRANCISCO, SYLVAIN', 'ZAL', 'Zalgiris Kaunas', '2025-08-01', 'Guard')""")
    con.execute("INSERT INTO player_season_stats VALUES ('E2025', '001', 'ZAL', 1166.6, 42, 28.21)")
    con.execute("INSERT INTO fantasy_points_game VALUES ('E2025', '001', 1, 19.75)")
    # He is on Panathinaikos' announced roster, so absent from Zalgiris'.
    con.execute("""INSERT INTO announced_rosters VALUES
        ('E2026', 'PAN', 'Panathinaikos AKTOR Athens', '001', 'FRANCISCO, SYLVAIN',
         'Guard', '10', '2027-06-30', true)""")

    rows = con.execute(
        DEPARTURES_SQL, ["E2025", "E2025", "E2025", "ZAL", "E2026", "ZAL"]
    ).fetchall()
    assert [r[1] for r in rows] == ["FRANCISCO, SYLVAIN"]
    assert rows[0][2] == pytest.approx(27.8, abs=0.1), "minutes freed at the old club"


def test_a_person_code_is_not_history(con) -> None:
    """Valanciunas returns with a code from 2011-13 and no rows in this warehouse.

    The squad view must say so rather than let the empty average read as zero.
    """
    from euroleague_open_data.rosters import SQUAD_SQL

    _stats_tables(con)
    con.execute("""INSERT INTO announced_rosters VALUES
        ('E2026', 'ZAL', 'Zalgiris Kaunas', '000898', 'VALANCIUNAS, JONAS',
         'Center', '17', '2027-06-30', true)""")

    rows = con.execute(SQUAD_SQL, ["E2025", "E2025", "E2025", "E2026", "ZAL"]).fetchall()
    assert len(rows) == 1
    assert rows[0][-1] == "no_history_in_warehouse"
    assert rows[0][5] is None, "no fantasy average may be manufactured"


def test_squad_outlook_surfaces_a_failed_query_instead_of_an_empty_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken query once reported Zalgiris as having lost nobody."""
    from euroleague_open_data import mcp_server

    calls = {"n": 0}

    def fake_query(sql, params=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"rows": [{"n": 15}]}          # roster present
        return {"error": "Binder Error: Referenced table \"s\" not found!"}

    monkeypatch.setattr(mcp_server, "_query", fake_query)
    out = mcp_server.get_squad_outlook("ZAL")
    assert "error" in out
    assert "departed" not in out, "a failure must not be dressed up as an empty section"
