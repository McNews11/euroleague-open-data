"""One entry point for the whole pipeline: crawl -> build -> validate -> analytics -> export."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import duckdb

from . import analytics, validate, warehouse
from .crawl import crawl_season

log = logging.getLogger(__name__)

ATTRIBUTION = (
    "Data origin: Euroleague Basketball. Retrieved from publicly accessible endpoints. "
    "Unofficial, not affiliated with or endorsed by Euroleague Basketball. "
    "Research and educational use. See DISCLAIMER.md."
)


def export(db_path: Path, out_dir: Path) -> list[Path]:
    """Write every table to Parquet and CSV, with an attribution file alongside."""
    out_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path), read_only=True)
    tables = [
        r[0]
        for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main' "
            "ORDER BY table_name"
        ).fetchall()
    ]

    written: list[Path] = []
    for table in tables:
        for fmt in ("parquet", "csv"):
            path = out_dir / f"{table}.{fmt}"
            if fmt == "parquet":
                con.execute(f"COPY {table} TO '{path}' (FORMAT PARQUET, COMPRESSION ZSTD)")
            else:
                con.execute(f"COPY {table} TO '{path}' (FORMAT CSV, HEADER)")
            written.append(path)
        log.info("exported %s", table)
    con.close()

    (out_dir / "ATTRIBUTION.txt").write_text(ATTRIBUTION + "\n")
    written.append(out_dir / "ATTRIBUTION.txt")
    return written


def main() -> None:
    parser = argparse.ArgumentParser(prog="euroleague-etl", description="EuroLeague ETL pipeline.")
    parser.add_argument("--competition", default="E", choices=["E", "U"])
    parser.add_argument("--season", default="E2025")
    parser.add_argument("--cache-dir", type=Path, default=Path("data/cache"))
    parser.add_argument("--db", type=Path, default=Path("data/euroleague.duckdb"))
    parser.add_argument("--exports", type=Path, default=Path("data/exports"))
    parser.add_argument("--report", type=Path, default=Path("docs/data-quality-report.json"))
    parser.add_argument(
        "--skip-crawl",
        action="store_true",
        help="rebuild from the existing cache without contacting upstream",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S"
    )

    if not args.skip_crawl:
        report = crawl_season(args.competition, args.season, args.cache_dir)
        log.info("crawl: %s", json.dumps(report))

    warehouse.build(args.cache_dir, args.db, args.competition, args.season)
    analytics.build(args.db)

    quality = validate.run_all(args.db)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(quality, indent=2))
    log.info(
        "validation: %s (%d blocking failures)", quality["status"], quality["blocking_failures"]
    )

    export(args.db, args.exports)

    if quality["status"] == "FAIL":
        raise SystemExit("validation failed -- refusing to declare success")


if __name__ == "__main__":
    main()
