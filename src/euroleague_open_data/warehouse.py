"""Parse the raw cache into a DuckDB warehouse.

Reads only from the on-disk cache written by crawl.py -- never from the network. That
separation means a schema change costs a re-parse (seconds) instead of a re-crawl (hours
at the upstream's ~10 req/min ceiling).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from .sources import strip_id

log = logging.getLogger(__name__)

# The 24 stat fields upstream returns for both players and teams, in a stable order.
STAT_FIELDS = [
    "timePlayed",
    "valuation",
    "points",
    "fieldGoalsMade2",
    "fieldGoalsAttempted2",
    "fieldGoalsMade3",
    "fieldGoalsAttempted3",
    "freeThrowsMade",
    "freeThrowsAttempted",
    "fieldGoalsMadeTotal",
    "fieldGoalsAttemptedTotal",
    "totalRebounds",
    "defensiveRebounds",
    "offensiveRebounds",
    "assistances",
    "steals",
    "turnovers",
    "blocksFavour",
    "blocksAgainst",
    "foulsCommited",
    "foulsReceived",
    "plusMinus",
]

# snake_case names for the same fields, used as column names.
STAT_COLUMNS = [
    "seconds_played",
    "valuation",
    "points",
    "fg2m",
    "fg2a",
    "fg3m",
    "fg3a",
    "ftm",
    "fta",
    "fgm",
    "fga",
    "rebounds_total",
    "rebounds_defensive",
    "rebounds_offensive",
    "assists",
    "steals",
    "turnovers",
    "blocks_favour",
    "blocks_against",
    "fouls_committed",
    "fouls_received",
    "plus_minus",
]

PERIOD_KEYS = {
    "FirstQuarter": 1,
    "SecondQuarter": 2,
    "ThirdQuarter": 3,
    "ForthQuarter": 4,  # upstream's spelling, not ours
    "ExtraTime": 5,  # all overtimes collapsed together upstream
}


class RawCache:
    """URL-keyed view over the crawl cache."""

    def __init__(self, cache_dir: Path) -> None:
        self._by_url: dict[str, Any] = {}
        for path in cache_dir.glob("*.json"):
            try:
                entry = json.loads(path.read_text())
            except json.JSONDecodeError:
                log.warning("skipping corrupt cache entry %s", path)
                continue
            self._by_url[entry["url"]] = entry["body"]
        log.info("loaded %d cached responses", len(self._by_url))

    def get(self, url: str) -> Any:
        return self._by_url.get(url)

    def find(self, *fragments: str) -> Iterator[tuple[str, Any]]:
        for url, body in self._by_url.items():
            if all(f in url for f in fragments):
                yield url, body


# Non-player actors that appear in the play-by-play person slot. They are legitimate
# events (coach technicals, bench fouls), not broken rows, and must not be counted as
# crosswalk failures.
NON_PLAYER_ACTORS = {"CO_A", "CO_B"}


def _person_key_v2(raw: str | None) -> str | None:
    """Identifier as it appears in v2 boxscores and the people endpoint: '006590', 'TGB'."""
    return strip_id(raw)


def _person_key_live(raw: str | None) -> str | None:
    """Identifier as it appears in play-by-play and shots: 'P006590   ', 'PTGB      '.

    The live endpoints namespace every person code with a literal 'P' prefix. Strip
    exactly one, unconditionally -- do NOT make this conditional on the remainder being
    numeric. Sergio Llull's code is 'TGB' in v2 and 'PTGB' in the live feed, so a
    digits-only rule silently splits him into two people. Discovered on E2025 game 13.

    See docs/api-notes.md section 5.2.
    """
    cleaned = strip_id(raw)
    if cleaned is None:
        return None
    if cleaned in NON_PLAYER_ACTORS:
        return cleaned
    if cleaned.startswith("P") and len(cleaned) > 1:
        return cleaned[1:]
    return cleaned


def _stats_row(stats: dict[str, Any]) -> dict[str, Any]:
    return {col: stats.get(src) for col, src in zip(STAT_COLUMNS, STAT_FIELDS, strict=True)}


@dataclass
class Extraction:
    seasons: list[dict[str, Any]]
    teams: list[dict[str, Any]]
    players: list[dict[str, Any]]
    games: list[dict[str, Any]]
    boxscores_player: list[dict[str, Any]]
    boxscores_team: list[dict[str, Any]]
    shots: list[dict[str, Any]]
    play_by_play: list[dict[str, Any]]
    coaches: list[dict[str, Any]]
    player_team_spells: list[dict[str, Any]]


def extract(cache: RawCache, comp: str, season: str) -> Extraction:
    ex = Extraction([], [], [], [], [], [], [], [], [], [])

    # -- seasons ---------------------------------------------------------------
    for _url, body in cache.find(f"/competitions/{comp}/seasons"):
        if isinstance(body, dict) and "data" in body and "/seasons/" not in _url:
            for s in body["data"]:
                ex.seasons.append(
                    {
                        "season_code": s["code"],
                        "competition_code": s.get("competitionCode"),
                        "name": s.get("name"),
                        "alias": s.get("alias"),
                        "year": s.get("year"),
                        "start_date": s.get("startDate"),
                        "end_date": s.get("endDate"),
                    }
                )
            break

    # -- teams -----------------------------------------------------------------
    for _url, body in cache.find(f"/seasons/{season}/clubs"):
        for c in body.get("data", []):
            country = c.get("country") or {}
            ex.teams.append(
                {
                    "season_code": season,
                    "team_code": c.get("code"),
                    "name": c.get("name"),
                    "abbreviated_name": c.get("abbreviatedName"),
                    "tv_code": c.get("tvCode"),
                    "club_permanent_name": c.get("clubPermanentName"),
                    "club_permanent_alias": c.get("clubPermanentAlias"),
                    "country_code": country.get("code"),
                    "country_name": country.get("name"),
                    "is_virtual": c.get("isVirtual"),
                    "crest_url": (c.get("images") or {}).get("crest"),
                }
            )
        break

    # -- games -----------------------------------------------------------------
    game_codes: list[int] = []
    for _url, body in cache.find(f"/seasons/{season}/games?limit"):
        for g in body.get("data", []):
            local, road = g.get("local") or {}, g.get("road") or {}
            venue = g.get("venue") or {}
            phase = g.get("phaseType") or {}
            lp = local.get("partials") or {}
            rp = road.get("partials") or {}
            code = g.get("gameCode")
            if isinstance(code, int):
                game_codes.append(code)
            ex.games.append(
                {
                    "season_code": season,
                    "competition_code": comp,
                    "game_code": code,
                    "identifier": g.get("identifier"),
                    "round": g.get("round"),
                    "round_name": g.get("roundName"),
                    "phase_code": phase.get("code"),
                    "phase_name": phase.get("name"),
                    "group_name": (g.get("group") or {}).get("rawName"),
                    "played": g.get("played"),
                    "utc_date": g.get("utcDate"),
                    "local_date": g.get("localDate"),
                    "home_team_code": (local.get("club") or {}).get("code"),
                    "away_team_code": (road.get("club") or {}).get("code"),
                    "home_score": local.get("score"),
                    "away_score": road.get("score"),
                    "home_q1": lp.get("partials1"),
                    "home_q2": lp.get("partials2"),
                    "home_q3": lp.get("partials3"),
                    "home_q4": lp.get("partials4"),
                    "away_q1": rp.get("partials1"),
                    "away_q2": rp.get("partials2"),
                    "away_q3": rp.get("partials3"),
                    "away_q4": rp.get("partials4"),
                    "venue_name": venue.get("name"),
                    "venue_capacity": venue.get("capacity"),
                    "is_neutral_venue": g.get("isNeutralVenue"),
                    "attendance": g.get("audience"),
                    "referee_1": (g.get("referee1") or {}).get("name"),
                    "referee_2": (g.get("referee2") or {}).get("name"),
                    "referee_3": (g.get("referee3") or {}).get("name"),
                }
            )
        break

    # -- boxscores -------------------------------------------------------------
    seen_players: set[str] = set()

    for url, body in cache.find(f"/seasons/{season}/games/", "/stats"):
        code = int(url.rsplit("/games/", 1)[1].split("/")[0])
        if not isinstance(body, dict):
            continue
        for side, is_home in (("local", True), ("road", False)):
            block = body.get(side)
            if not isinstance(block, dict):
                continue

            team_code: str | None = None
            for entry in block.get("players") or []:
                player = entry.get("player") or {}
                person = player.get("person") or {}
                club = player.get("club") or {}
                person_code = _person_key_v2(person.get("code"))
                team_code = team_code or club.get("code")
                if person_code is None:
                    continue

                if person_code not in seen_players:
                    seen_players.add(person_code)
                    country = person.get("country") or {}
                    ex.players.append(
                        {
                            "person_code": person_code,
                            "name": person.get("name"),
                            "alias": person.get("alias"),
                            "passport_name": person.get("passportName"),
                            "passport_surname": person.get("passportSurname"),
                            "country_code": country.get("code"),
                            "country_name": country.get("name"),
                            "height_cm": person.get("height") or None,
                            "weight_kg": person.get("weight") or None,
                            "birth_date": person.get("birthDate"),
                            "position": player.get("position"),
                            "position_name": player.get("positionName"),
                        }
                    )

                row = {
                    "season_code": season,
                    "game_code": code,
                    "team_code": club.get("code"),
                    "person_code": person_code,
                    "is_home": is_home,
                    "dorsal": player.get("dorsal"),
                    "is_starter": entry.get("stats", {}).get("timePlayed", 0) > 0
                    and player.get("order") is not None,
                }
                row.update(_stats_row(entry.get("stats") or {}))
                ex.boxscores_player.append(row)

            totals = block.get("total")
            if isinstance(totals, dict):
                trow = {
                    "season_code": season,
                    "game_code": code,
                    "team_code": team_code,
                    "is_home": is_home,
                }
                trow.update(_stats_row(totals))
                ex.boxscores_team.append(trow)

            # One row per team per game, so a mid-season coaching change is visible as a
            # change in this column rather than being flattened into a season-level fact.
            coach = block.get("coach")
            if isinstance(coach, dict) and coach.get("code"):
                ex.coaches.append(
                    {
                        "season_code": season,
                        "game_code": code,
                        "team_code": team_code,
                        "is_home": is_home,
                        "coach_code": strip_id(coach.get("code")),
                        "coach_name": coach.get("name"),
                    }
                )

    # -- roster spells ---------------------------------------------------------
    # The people endpoint carries startDate/endDate per club, plus `lastTeam` -- which is
    # what makes it possible to see that a player arrived from somewhere, and when. A
    # startDate in midwinter is a mid-season signing, not a summer transfer.
    for _url, body in cache.find(f"/seasons/{season}/people"):
        for entry in (body or {}).get("data", []):
            if entry.get("type") != "J":  # J = player; the feed also carries staff
                continue
            person = entry.get("person") or {}
            club = entry.get("club") or {}
            person_code = _person_key_v2(person.get("code"))
            if person_code is None:
                continue
            ex.player_team_spells.append(
                {
                    "season_code": season,
                    "person_code": person_code,
                    "player_name": person.get("name"),
                    "team_code": club.get("code"),
                    "team_name": club.get("name"),
                    "previous_team": entry.get("lastTeam"),
                    "start_date": entry.get("startDate"),
                    "end_date": entry.get("endDate"),
                    "is_active": entry.get("active"),
                    "dorsal": entry.get("dorsal"),
                    "position": entry.get("position"),
                    "position_name": entry.get("positionName"),
                }
            )
        break

    # -- shots -----------------------------------------------------------------
    for url, body in cache.find("/api/Points", f"seasoncode={season}"):
        code = int(url.split("gamecode=")[1].split("&")[0])
        rows = (body or {}).get("Rows") or []
        for r in rows:
            ex.shots.append(
                {
                    "season_code": season,
                    "game_code": code,
                    "sequence": r.get("NUM_ANOT"),
                    "team_code": strip_id(r.get("TEAM")),
                    "person_code": _person_key_live(r.get("ID_PLAYER")),
                    "player_name": r.get("PLAYER"),
                    "action_id": strip_id(r.get("ID_ACTION")),
                    "action": r.get("ACTION"),
                    "points": r.get("POINTS"),
                    "coord_x": r.get("COORD_X"),
                    "coord_y": r.get("COORD_Y"),
                    "zone": strip_id(r.get("ZONE")),
                    "is_fastbreak": str(r.get("FASTBREAK", "0")).strip() == "1",
                    "is_second_chance": str(r.get("SECOND_CHANCE", "0")).strip() == "1",
                    "is_points_off_turnover": str(r.get("POINTS_OFF_TURNOVER", "0")).strip() == "1",
                    "minute": r.get("MINUTE"),
                    "console_clock": strip_id(r.get("CONSOLE")),
                    "score_home": r.get("POINTS_A"),
                    "score_away": r.get("POINTS_B"),
                    "utc_raw": strip_id(r.get("UTC")),
                }
            )

    # -- play by play ----------------------------------------------------------
    for url, body in cache.find("/api/PlayByPlay", f"seasoncode={season}"):
        code = int(url.split("gamecode=")[1].split("&")[0])
        if not isinstance(body, dict):
            continue
        for key, period in PERIOD_KEYS.items():
            for e in body.get(key) or []:
                ex.play_by_play.append(
                    {
                        "season_code": season,
                        "game_code": code,
                        "period": period,
                        "period_source": key,
                        "play_number": e.get("NUMBEROFPLAY"),
                        "play_type": strip_id(e.get("PLAYTYPE")),
                        "play_info": e.get("PLAYINFO"),
                        "team_code": strip_id(e.get("CODETEAM")),
                        "person_code": _person_key_live(e.get("PLAYER_ID")),
                        "player_name": e.get("PLAYER"),
                        "dorsal": strip_id(e.get("DORSAL")),
                        "minute": e.get("MINUTE"),
                        "marker_time": strip_id(e.get("MARKERTIME")),
                        "score_home": e.get("POINTS_A"),
                        "score_away": e.get("POINTS_B"),
                        "comment": e.get("COMMENT"),
                    }
                )

    log.info(
        "extracted: %d games, %d player boxscore rows, %d team rows, %d shots, %d pbp events",
        len(ex.games),
        len(ex.boxscores_player),
        len(ex.boxscores_team),
        len(ex.shots),
        len(ex.play_by_play),
    )
    return ex


def build(cache_dir: Path, db_path: Path, comp: str, season: str) -> Path:
    cache = RawCache(cache_dir)
    ex = extract(cache, comp, season)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    con = duckdb.connect(str(db_path))

    for table, rows in [
        ("seasons", ex.seasons),
        ("teams", ex.teams),
        ("players", ex.players),
        ("games", ex.games),
        ("boxscores_player", ex.boxscores_player),
        ("boxscores_team", ex.boxscores_team),
        ("shots", ex.shots),
        ("play_by_play", ex.play_by_play),
        ("coaches", ex.coaches),
        ("player_team_spells", ex.player_team_spells),
    ]:
        if not rows:
            log.warning("table %s is empty -- skipping", table)
            continue
        con.register("staging", _to_arrow(rows))
        con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM staging")
        con.unregister("staging")
        count = con.execute(f"SELECT count(*) FROM {table}").fetchone()
        log.info("%-18s %s rows", table, count[0] if count else "?")

    con.execute("ALTER TABLE games ADD COLUMN IF NOT EXISTS data_source VARCHAR")
    con.execute("UPDATE games SET data_source = 'euroleague-basketball'")
    con.close()
    return db_path


def _to_arrow(rows: list[dict[str, Any]]) -> Any:
    import pyarrow as pa

    keys: list[str] = []
    for row in rows:
        for k in row:
            if k not in keys:
                keys.append(k)
    columns = {k: [r.get(k) for r in rows] for k in keys}
    return pa.table(columns)
