"""run_sql is the escape hatch that makes the server useful for unanticipated questions.

It is also the only tool that takes free-form input from a model, so its guardrails are
tested rather than assumed.
"""

import duckdb
import pytest

from euroleague_open_data import mcp_server


@pytest.fixture(autouse=True)
def tiny_warehouse(tmp_path, monkeypatch):
    db = tmp_path / "test.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE games AS SELECT 1 AS game_code, 'E2025' AS season_code")
    con.close()

    monkeypatch.setenv("EUROLEAGUE_DB", str(db))
    monkeypatch.setattr(mcp_server, "_connection", None)
    yield
    monkeypatch.setattr(mcp_server, "_connection", None)


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TABLE games",
        "DELETE FROM games",
        "UPDATE games SET game_code = 2",
        "CREATE TABLE evil AS SELECT 1",
        "ATTACH 'other.duckdb'",
        "COPY games TO '/tmp/leak.csv'",
        "INSTALL httpfs",
        "PRAGMA database_list",
    ],
)
def test_mutating_and_side_effecting_statements_are_refused(statement):
    result = mcp_server.run_sql(statement)
    assert "error" in result, f"{statement!r} should have been refused"


def test_stacked_statements_are_refused():
    """A second statement is where an injected write would hide."""
    assert "error" in mcp_server.run_sql("SELECT 1; DROP TABLE games")


def test_plain_select_is_allowed():
    result = mcp_server.run_sql("SELECT game_code FROM games")
    assert result.get("row_count") == 1
    assert result["rows"][0]["game_code"] == 1


def test_cte_is_allowed():
    result = mcp_server.run_sql("WITH x AS (SELECT 1 AS n) SELECT n FROM x")
    assert result.get("row_count") == 1


def test_row_limit_is_capped():
    result = mcp_server.run_sql("SELECT * FROM games", limit=10_000)
    assert result["row_count"] <= mcp_server.MAX_ROWS
