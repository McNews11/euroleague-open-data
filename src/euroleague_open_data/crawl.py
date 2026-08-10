"""Season crawler: fetches raw upstream JSON into the permanent cache.

Fetching is kept separate from parsing on purpose. At ~10 requests/minute a re-crawl of a
single season costs two hours; a re-parse costs seconds. So this stage writes responses
verbatim and interprets nothing.

Safe to interrupt and rerun. Anything already cached costs no request and no delay.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from typing import Any

from . import sources
from .http import ThrottledClient

log = logging.getLogger(__name__)


def _paginate_games(client: ThrottledClient, comp: str, season: str) -> list[dict[str, Any]]:
    """Collect the full game list, following `total` rather than trusting one page."""
    collected: list[dict[str, Any]] = []
    offset = 0
    total: int | None = None

    while True:
        payload = client.get_json(sources.games(comp, season, limit=500, offset=offset))
        if not payload:
            break
        page = payload.get("data") or []
        total = payload.get("total") if total is None else total
        collected.extend(page)
        if not page or (total is not None and len(collected) >= total):
            break
        offset += len(page)

    if total is not None and len(collected) != total:
        log.warning("game list incomplete: got %d of %d", len(collected), total)
    return collected


def crawl_season(
    comp: str,
    season: str,
    cache_dir: Path,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    """Fetch every artefact needed to build the warehouse for one season."""
    started = time.time()

    with ThrottledClient(cache_dir) as client:
        log.info("season structure: %s %s", comp, season)
        client.get_json(sources.seasons(comp))
        client.get_json(sources.clubs(comp, season))
        client.get_json(sources.people(comp, season))

        game_list = _paginate_games(client, comp, season)
        log.info("%s: %d games in schedule", season, len(game_list))

        codes = sorted(g["gameCode"] for g in game_list if isinstance(g.get("gameCode"), int))
        if limit is not None:
            codes = codes[:limit]

        played = 0
        skipped_unplayed = 0

        for index, code in enumerate(codes, start=1):
            stats = client.get_json(sources.game_stats(comp, season, code))

            # An unplayed fixture returns a structurally valid but empty stats payload.
            # Crawling its play-by-play would spend two requests to learn nothing.
            if not _looks_played(stats):
                skipped_unplayed += 1
                continue

            client.get_json(sources.play_by_play(season, code))
            client.get_json(sources.points(season, code))
            played += 1

            if index % 25 == 0:
                s = client.stats
                log.info(
                    "%s %d/%d games | %d requests, %d cached, %d rate-limits, %.1fs slept",
                    season,
                    index,
                    len(codes),
                    s.requests,
                    s.cache_hits,
                    s.rate_limit_hits,
                    s.sleep_seconds,
                )

        report = {
            "competition": comp,
            "season": season,
            "games_in_schedule": len(game_list),
            "games_crawled": played,
            "games_skipped_unplayed": skipped_unplayed,
            "elapsed_seconds": round(time.time() - started, 1),
            "fetch": client.stats.as_dict(),
        }

    return report


def _looks_played(stats: Any) -> bool:
    if not isinstance(stats, dict):
        return False
    local = stats.get("local")
    if not isinstance(local, dict):
        return False
    players = local.get("players")
    return bool(players)


def main() -> None:
    parser = argparse.ArgumentParser(description="Crawl one EuroLeague/EuroCup season.")
    parser.add_argument("--competition", default="E", choices=["E", "U"])
    parser.add_argument("--season", default="E2025")
    parser.add_argument("--cache-dir", type=Path, default=Path("data/cache"))
    parser.add_argument("--report", type=Path, default=Path("data/crawl-report.json"))
    parser.add_argument("--limit", type=int, default=None, help="crawl only the first N games")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    report = crawl_season(args.competition, args.season, args.cache_dir, limit=args.limit)

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2))
    log.info("done: %s", json.dumps(report))


if __name__ == "__main__":
    main()
