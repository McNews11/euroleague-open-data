"""BasketNews fantasy scoring, checked against hand-computed lines.

The draft board is only as good as this formula, so each case below is worked out by hand
in the comment and then asserted, rather than being compared to whatever the code happens
to produce.
"""

import duckdb
import pytest

from euroleague_open_data import fantasy


def _warehouse(tmp_path, players):
    """Build a minimal warehouse. `players` is a list of stat dicts."""
    db = tmp_path / "f.duckdb"
    con = duckdb.connect(str(db))
    con.execute("""
        CREATE TABLE games (
            season_code VARCHAR, game_code BIGINT, home_score INTEGER,
            away_score INTEGER, utc_date VARCHAR, round INTEGER
        )
    """)
    con.execute("INSERT INTO games VALUES ('E2025', 1, 90, 80, '2025-10-01T18:00:00Z', 1)")
    con.execute("""
        CREATE TABLE players (person_code VARCHAR, name VARCHAR,
                              position_name VARCHAR, height_cm INTEGER, country_name VARCHAR)
    """)
    con.execute(
        "CREATE TABLE boxscores_player (season_code VARCHAR, game_code BIGINT, "
        "person_code VARCHAR, team_code VARCHAR, is_home BOOLEAN, "
        "seconds_played DOUBLE, points DOUBLE, assists DOUBLE, steals DOUBLE, "
        "rebounds_total DOUBLE, rebounds_offensive DOUBLE, rebounds_defensive DOUBLE, "
        "blocks_favour DOUBLE, blocks_against DOUBLE, turnovers DOUBLE, "
        "fouls_committed DOUBLE, fouls_received DOUBLE, valuation DOUBLE, "
        "fgm DOUBLE, fga DOUBLE, ftm DOUBLE, fta DOUBLE)"
    )

    for i, p in enumerate(players):
        con.execute(
            "INSERT INTO players VALUES (?, ?, 'Guard', 190, 'Lithuania')",
            [p["code"], f"PLAYER {i}"],
        )
        con.execute(
            "INSERT INTO boxscores_player VALUES ('E2025', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                p["code"],
                p.get("team", "AAA"),
                p.get("is_home", True),
                p.get("seconds", 1800.0),
                p["points"],
                p.get("assists", 0.0),
                p.get("steals", 0.0),
                p.get("reb_total", 0.0),
                p.get("reb_off", 0.0),
                p.get("reb_def", 0.0),
                p.get("blocks", 0.0),
                p.get("blocks_against", 0.0),
                p.get("turnovers", 0.0),
                p.get("fouls_committed", 0.0),
                p.get("fouls_received", 0.0),
                p.get("pir", 0.0),
                p.get("fgm", 0.0),
                p.get("fga", 0.0),
                p.get("ftm", 0.0),
                p.get("fta", 0.0),
            ],
        )
    con.close()
    return db


def _score(db, code):
    con = duckdb.connect(str(db), read_only=True)
    row = con.execute(
        "SELECT fantasy_modern, fantasy_classic FROM fantasy_points_game WHERE person_code = ?",
        [code],
    ).fetchone()
    con.close()
    return row


def test_full_line_with_double_double(tmp_path):
    """12 pts, 11 reb (5 off / 6 def), 3 ast, 1 stl, 2 TO, 4 fouls drawn,
    10 FGA-5 FGM, 4 FTA-2 FTM, team won.

        points          12 * 1.0   = 12.0
        def rebounds     6 * 1.0   =  6.0
        off rebounds     5 * 1.5   =  7.5
        assists          3 * 1.5   =  4.5
        steals           1 * 1.5   =  1.5
        fouls drawn      4 * 1.0   =  4.0
        missed FG        5 * -1.0  = -5.0
        missed FT        2 * -1.0  = -2.0
        turnovers        2 * -1.5  = -3.0
        double-double              = 10.0
        win                        =  1.5
                                    ------
                                     37.0
    """
    db = _warehouse(
        tmp_path,
        [
            {
                "code": "001",
                "points": 12.0,
                "reb_total": 11.0,
                "reb_off": 5.0,
                "reb_def": 6.0,
                "assists": 3.0,
                "steals": 1.0,
                "turnovers": 2.0,
                "fouls_received": 4.0,
                "fouls_committed": 3.0,
                "fgm": 5.0,
                "fga": 10.0,
                "ftm": 2.0,
                "fta": 4.0,
                "pir": 20.0,
            }
        ],
    )
    fantasy.build(db, teams=8, roster_size=13)
    modern, classic = _score(db, "001")
    assert modern == pytest.approx(37.0)
    # Classic: PIR 20 on a win -> 20 * 1.1
    assert classic == pytest.approx(22.0)


def test_loss_flips_both_systems(tmp_path):
    """Same line as above but on the losing side: -1.5 instead of +1.5, and PIR * 0.9."""
    db = _warehouse(
        tmp_path,
        [
            {
                "code": "002",
                "points": 12.0,
                "reb_total": 11.0,
                "reb_off": 5.0,
                "reb_def": 6.0,
                "assists": 3.0,
                "steals": 1.0,
                "turnovers": 2.0,
                "fouls_received": 4.0,
                "fouls_committed": 3.0,
                "fgm": 5.0,
                "fga": 10.0,
                "ftm": 2.0,
                "fta": 4.0,
                "pir": 20.0,
                "is_home": False,
            }
        ],
    )
    fantasy.build(db, teams=8, roster_size=13)
    modern, classic = _score(db, "002")
    assert modern == pytest.approx(34.0)  # 37.0 - 1.5 - 1.5
    assert classic == pytest.approx(18.0)  # 20 * 0.9


def test_five_fouls_penalty(tmp_path):
    """10 points on a win, fouled out: 10 + 1.5 - 5 = 6.5."""
    db = _warehouse(
        tmp_path,
        [
            {
                "code": "003",
                "points": 10.0,
                "fouls_committed": 5.0,
                "fgm": 5.0,
                "fga": 5.0,
            }
        ],
    )
    fantasy.build(db, teams=8, roster_size=13)
    modern, _ = _score(db, "003")
    assert modern == pytest.approx(6.5)


def test_triple_double_is_not_stacked_with_double_double(tmp_path):
    """10 pts, 10 reb, 10 ast on a win gets 30, not 40.

    10 + 10(def reb) + 15(ast) + 30(triple) + 1.5(win) = 66.5
    """
    db = _warehouse(
        tmp_path,
        [
            {
                "code": "004",
                "points": 10.0,
                "reb_total": 10.0,
                "reb_def": 10.0,
                "assists": 10.0,
                "fgm": 5.0,
                "fga": 5.0,
            }
        ],
    )
    fantasy.build(db, teams=8, roster_size=13)
    modern, _ = _score(db, "004")
    assert modern == pytest.approx(66.5)


def test_did_not_play_scores_zero_including_win_bonus(tmp_path):
    """A DNP on a winning team must not collect +1.5, or every bench player on a good
    team would look draftable."""
    db = _warehouse(tmp_path, [{"code": "005", "points": 0.0, "seconds": 0.0, "pir": 0.0}])
    fantasy.build(db, teams=8, roster_size=13)
    modern, classic = _score(db, "005")
    assert modern == 0
    assert classic == 0


def test_blocks_against_are_penalised(tmp_path):
    """8 points, 2 shots blocked, win: 8 + 1.5 - 1.0 = 8.5 (2 blocked shots also count
    as missed field goals, which is the -2 already inside FGA-FGM)."""
    db = _warehouse(
        tmp_path,
        [
            {
                "code": "006",
                "points": 8.0,
                "blocks_against": 2.0,
                "fgm": 4.0,
                "fga": 4.0,
            }
        ],
    )
    fantasy.build(db, teams=8, roster_size=13)
    modern, _ = _score(db, "006")
    assert modern == pytest.approx(8.5)


def test_season_aggregate_excludes_dnp_games(tmp_path):
    db = _warehouse(
        tmp_path,
        [
            {"code": "007", "points": 20.0, "fgm": 10.0, "fga": 10.0},
            {"code": "008", "points": 0.0, "seconds": 0.0},
        ],
    )
    fantasy.build(db, teams=8, roster_size=13)
    con = duckdb.connect(str(db), read_only=True)
    rows = dict(
        con.execute("SELECT person_code, games_played FROM fantasy_player_season").fetchall()
    )
    con.close()
    assert rows.get("007") == 1
    assert "008" not in rows


def test_draft_board_is_scoped_per_season(tmp_path):
    """A draft happens inside one season's player pool. Ranking across every loaded
    season at once inflates the pool and understates each player's edge over replacement
    -- the E2025 leader dropped from 1st to 5th and lost 5.5 points of VORP once EuroCup
    seasons were loaded alongside."""
    db = tmp_path / "seasons.duckdb"
    con = duckdb.connect(str(db))
    con.execute("""
        CREATE TABLE fantasy_player_season (
            season_code VARCHAR, person_code VARCHAR, player_name VARCHAR,
            team_code VARCHAR, position_name VARCHAR, games_played BIGINT,
            minutes_per_game DOUBLE, modern_per_game DOUBLE, classic_per_game DOUBLE,
            modern_total DOUBLE, modern_stddev DOUBLE, modern_floor_p25 DOUBLE,
            modern_ceiling_p75 DOUBLE, consistency_ratio DOUBLE, double_doubles BIGINT
        )
    """)
    # Season A's players all score higher than season B's. Scoped correctly, each season
    # has its own rank 1; pooled, season B would have no top-ranked player at all.
    rows = []
    for i in range(12):
        rows.append(
            (
                "A",
                f"a{i}",
                f"A{i}",
                "AAA",
                "Guard",
                20,
                25.0,
                40.0 - i,
                40.0 - i,
                0.0,
                1.0,
                1.0,
                1.0,
                1.0,
                0,
            )
        )
        rows.append(
            (
                "B",
                f"b{i}",
                f"B{i}",
                "BBB",
                "Guard",
                20,
                25.0,
                20.0 - i,
                20.0 - i,
                0.0,
                1.0,
                1.0,
                1.0,
                1.0,
                0,
            )
        )
    con.executemany(
        "INSERT INTO fantasy_player_season VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )

    board = con.execute(
        "SELECT season_code, overall_rank, player_name FROM ("
        + fantasy.draft_board_select(4, 3, "classic")
        + ") WHERE overall_rank = 1 ORDER BY season_code"
    ).fetchall()
    con.close()

    assert [(r[0], r[2]) for r in board] == [("A", "A0"), ("B", "B0")]
