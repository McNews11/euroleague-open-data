"""Manual corrections for what the data cannot know.

Every failure mode here is silent by nature: a typo'd name, an ambiguous fragment, a
status nobody recognises. Each one would leave a player on the draft board that the user
had explicitly removed, while the file on disk says otherwise. So they all raise.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from euroleague_open_data import overrides


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(":memory:")
    c.execute("CREATE TABLE players (person_code VARCHAR, name VARCHAR)")
    c.execute("""INSERT INTO players VALUES
        ('001', 'DIALLO, ALPHA'),
        ('002', 'WALKER IV, LONNIE'),
        ('003', 'WRIGHT, MOSES'),
        ('004', 'WRIGHT IV, MCKINLEY')""")
    return c


def write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "overrides.csv"
    p.write_text("player,status,note\n" + body, encoding="utf-8")
    return p


def test_override_is_applied(con, tmp_path: Path) -> None:
    path = write(tmp_path, '"DIALLO, ALPHA",left_league,went to the NBA\n')
    applied = overrides.load(con, path)
    assert len(applied) == 1
    row = con.execute("SELECT person_code, status FROM player_overrides").fetchone()
    assert row == ("001", "left_league")


def test_a_name_matching_nobody_raises(con, tmp_path: Path) -> None:
    """A typo must not pass as 'nothing to do'."""
    path = write(tmp_path, "DIALO ALPHA,left_league,typo\n")
    with pytest.raises(overrides.OverrideError, match="matches no player"):
        overrides.load(con, path)


def test_an_ambiguous_name_raises(con, tmp_path: Path) -> None:
    """WRIGHT matches two players; picking one silently would be a coin flip."""
    path = write(tmp_path, "WRIGHT,left_league,which one\n")
    with pytest.raises(overrides.OverrideError, match="ambiguous"):
        overrides.load(con, path)


def test_unknown_status_raises(con, tmp_path: Path) -> None:
    path = write(tmp_path, '"DIALLO, ALPHA",gone,not a valid status\n')
    with pytest.raises(overrides.OverrideError, match="unknown status"):
        overrides.load(con, path)


def test_comments_are_stripped(con, tmp_path: Path) -> None:
    """The file carries its reasoning in comments; they must not become rows."""
    p = tmp_path / "overrides.csv"
    p.write_text(
        "# why this file exists\nplayer,status,note\n"
        '# a note about the next line\n"DIALLO, ALPHA",retired,hung them up\n',
        encoding="utf-8",
    )
    assert len(overrides.load(con, p)) == 1


def test_reloading_replaces_rather_than_accumulates(con, tmp_path: Path) -> None:
    path = write(tmp_path, '"DIALLO, ALPHA",left_league,first\n')
    overrides.load(con, path)
    path = write(tmp_path, '"WALKER IV, LONNIE",left_league,second\n')
    overrides.load(con, path)
    rows = con.execute("SELECT player_name FROM player_overrides").fetchall()
    assert [r[0] for r in rows] == ["WALKER IV, LONNIE"]


def test_available_status_does_not_exclude() -> None:
    assert "available" not in overrides.EXCLUDING
    assert {"left_league", "retired", "unavailable"} == overrides.EXCLUDING


def test_missing_file_is_not_an_error(con, tmp_path: Path) -> None:
    """No corrections is a legitimate state; the table is still created and emptied."""
    assert overrides.load(con, tmp_path / "nope.csv") == []
    assert con.execute("SELECT count(*) FROM player_overrides").fetchone()[0] == 0


def test_an_unquoted_note_with_a_comma_raises(con, tmp_path: Path) -> None:
    """Half a reason is worse than none, and DictReader loses it without complaint."""
    p = tmp_path / "overrides.csv"
    p.write_text(
        "player,status,note\n"
        '"DE COLO, NANDO",retired,retired after 2025-26, confirmed 2026-08-11\n',
        encoding="utf-8",
    )
    con.execute("INSERT INTO players VALUES ('005', 'DE COLO, NANDO')")
    with pytest.raises(overrides.OverrideError, match="too many columns"):
        overrides.load(con, p)


def test_a_quoted_note_with_a_comma_survives_intact(con, tmp_path: Path) -> None:
    p = tmp_path / "overrides.csv"
    p.write_text(
        "player,status,note\n"
        '"DE COLO, NANDO",retired,"retired after 2025-26; confirmed by Deividas, 2026-08-11"\n',
        encoding="utf-8",
    )
    con.execute("INSERT INTO players VALUES ('005', 'DE COLO, NANDO')")
    applied = overrides.load(con, p)
    assert applied[0]["note"].endswith("2026-08-11")
