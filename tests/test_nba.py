"""NBA cross-reference for players with no European history at all.

The risk here is not a missing row, it is a wrong one: two competitions share no
identifier, so the join is on a name. A shared surname must drop out rather than put a
stranger's season on someone's draft row.
"""

from __future__ import annotations

import duckdb
import pytest

from euroleague_open_data import nba


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
    c.execute("""CREATE TABLE nba_player_season (
        season VARCHAR, player_name VARCHAR, surname VARCHAR, forename VARCHAR,
        team VARCHAR, games_played INTEGER, minutes_per_game DOUBLE,
        points_per_game DOUBLE, nba_fantasy_per_game DOUBLE, euroleague_equivalent DOUBLE)""")
    return c


def roster(con, name: str, code: str = "") -> None:
    con.execute(
        "INSERT INTO announced_rosters VALUES ('E2026','ZAL','Zalgiris Kaunas',?,?,"
        "'Center','1','',false)",
        [code, name],
    )


def nba_row(con, surname: str, forename: str, fantasy: float = 20.0, gp: int = 60) -> None:
    con.execute(
        "INSERT INTO nba_player_season VALUES ('2025-26',?,?,?,'XXX',?,25.0,15.0,?,?)",
        [f"{forename} {surname}", surname, forename, gp, fantasy, round(fantasy * nba.NBA_FACTOR, 2)],
    )


def fetch(con):
    return con.execute(
        nba.NBA_FALLBACK_SQL, ["E2026", "2025-26", "2025-26", "2025-26"]
    ).fetchall()


def test_a_player_with_no_person_code_is_included(con) -> None:
    """The whole point: NBA arrivals have never been issued a code by this competition."""
    roster(con, "WARREN, TJ", code="")
    nba_row(con, "WARREN", "TJ")
    rows = fetch(con)
    assert len(rows) == 1
    assert rows[0][1] == "WARREN, TJ"


def test_the_conversion_is_applied_and_labelled(con) -> None:
    roster(con, "VALANCIUNAS, JONAS", code="000898")
    nba_row(con, "VALANCIUNAS", "JONAS", fantasy=17.13)
    row = fetch(con)[0]
    assert row[6] == pytest.approx(17.13 * nba.NBA_FACTOR, abs=0.02)
    assert row[10].startswith("NBA") and str(nba.NBA_FACTOR) in row[10]


def test_a_shared_name_is_dropped_not_guessed(con) -> None:
    """Two NBA players called the same thing means the join cannot identify anyone."""
    roster(con, "JONES, KAI")
    nba_row(con, "JONES", "KAI", fantasy=10.0)
    nba_row(con, "JONES", "KAI", fantasy=30.0)
    assert fetch(con) == []


def test_players_with_european_history_are_left_alone(con) -> None:
    roster(con, "VEZENKOV, SASHA", code="007")
    con.execute("INSERT INTO fantasy_points_game VALUES ('E2025','007',1,23.3)")
    nba_row(con, "VEZENKOV", "SASHA")
    assert fetch(con) == [], "a EuroLeague number must never be replaced by an NBA estimate"


def test_a_short_nba_season_is_not_used(con) -> None:
    roster(con, "CAMEO, PLAYER")
    nba_row(con, "CAMEO", "PLAYER", gp=nba.NBA_MIN_GAMES - 1)
    assert fetch(con) == []


def test_overrides_still_win(con) -> None:
    roster(con, "DIALLO, ALPHA")
    nba_row(con, "DIALLO", "ALPHA")
    con.execute("INSERT INTO player_overrides VALUES ('','DIALLO, ALPHA','left_league','NBA')")
    assert fetch(con) == []


def test_name_normalisation_survives_accents() -> None:
    assert nba.normalise("Nikola Jokić") == "NIKOLA JOKIC"
    assert nba.name_key("JOKIC, NIKOLA") == ("JOKIC", "NIKOLA")
