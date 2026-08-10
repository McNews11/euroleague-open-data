"""URL builders for the verified upstream endpoints.

Every route here was confirmed by live request. Routes that look plausible but are not
confirmed do not belong in this file. See docs/api-notes.md section 3.
"""

from __future__ import annotations

API_LIVE = "https://api-live.euroleague.net"
LIVE = "https://live.euroleague.net"


def seasons(comp: str) -> str:
    return f"{API_LIVE}/v2/competitions/{comp}/seasons"


def clubs(comp: str, season: str) -> str:
    return f"{API_LIVE}/v2/competitions/{comp}/seasons/{season}/clubs"


def people(comp: str, season: str, *, limit: int = 1000, offset: int = 0) -> str:
    return (
        f"{API_LIVE}/v2/competitions/{comp}/seasons/{season}/people?limit={limit}&offset={offset}"
    )


def games(comp: str, season: str, *, limit: int = 500, offset: int = 0) -> str:
    return f"{API_LIVE}/v2/competitions/{comp}/seasons/{season}/games?limit={limit}&offset={offset}"


def game_stats(comp: str, season: str, game_code: int) -> str:
    return f"{API_LIVE}/v2/competitions/{comp}/seasons/{season}/games/{game_code}/stats"


def play_by_play(season: str, game_code: int) -> str:
    """Not in any published spec. Events are bucketed per period, with 'ForthQuarter'
    spelled that way and all overtimes collapsed into a single 'ExtraTime' key."""
    return f"{LIVE}/api/PlayByPlay?gamecode={game_code}&seasoncode={season}"


def points(season: str, game_code: int) -> str:
    """Shot coordinates. Reverse-engineered; wrapped in {'Rows': [...]}. Data begins at
    the 2007 season in both competitions -- earlier seasons return no rows."""
    return f"{LIVE}/api/Points?gamecode={game_code}&seasoncode={season}"


def strip_id(raw: str | None) -> str | None:
    """Upstream space-pads identifiers: 'P002328   ', 'PAN       ', '          '.

    Returns None for an all-whitespace value, which is how upstream encodes "absent"
    rather than using null. See docs/api-notes.md section 5.2.
    """
    if raw is None:
        return None
    cleaned = raw.strip()
    return cleaned or None
