"""Human corrections for things the upstream data cannot express.

The API lists who is on a roster. It never says why someone is absent, so a player who
signed in the NBA is indistinguishable from one still negotiating -- both are simply
missing. That distinction decides whether a name belongs on a draft board at all, and no
amount of crawling will supply it.

So it comes from a person, through a committed CSV, and every row must resolve to exactly
one player. A row that matches nothing, or matches two people, raises rather than being
skipped: a correction that quietly fails to apply is worse than no correction, because it
looks like it worked.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any

import duckdb

log = logging.getLogger(__name__)

EXCLUDING = {"left_league", "retired", "unavailable"}
VALID = EXCLUDING | {"available"}


class OverrideError(ValueError):
    """A row that cannot be applied. Raised, never swallowed."""


def read_file(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with path.open(newline="", encoding="utf-8") as fh:
        # Comments carry the reasoning; strip them before the CSV reader sees them.
        lines = [ln for ln in fh if not ln.lstrip().startswith("#")]
    for row in csv.DictReader(lines):
        if not (row.get("player") or "").strip():
            continue
        # A note containing a comma splits into extra columns unless it is quoted, and
        # DictReader drops the remainder into restkey without complaint -- so "retired
        # after 2025-26 (Deividas, 2026-08-11)" silently became "retired after 2025-26
        # (Deividas". Losing half a reason is the same class of quiet failure this file
        # exists to prevent.
        if row.get(None):
            raise OverrideError(
                f"{row['player']}: too many columns. A note containing a comma must be "
                'quoted: "...,note with, commas"'
            )
        status = (row.get("status") or "").strip().lower()
        if status not in VALID:
            raise OverrideError(
                f"{row['player']}: unknown status {status!r}. Use one of {sorted(VALID)}."
            )
        rows.append(
            {
                "player": row["player"].strip(),
                "status": status,
                "note": (row.get("note") or "").strip(),
            }
        )
    return rows


def resolve(con: duckdb.DuckDBPyConnection, fragment: str) -> str:
    matches = con.execute(
        "SELECT person_code, name FROM players WHERE lower(name) LIKE lower('%' || ? || '%')",
        [fragment],
    ).fetchall()
    if not matches:
        raise OverrideError(
            f"{fragment!r} matches no player in the warehouse. Check the spelling, or "
            "note that players with no EuroLeague history are not in `players` at all."
        )
    if len(matches) > 1:
        names = ", ".join(n for _, n in matches)
        raise OverrideError(f"{fragment!r} is ambiguous: {names}. Use a longer fragment.")
    return str(matches[0][0])


def load(con: duckdb.DuckDBPyConnection, path: Path) -> list[dict[str, Any]]:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS player_overrides (
            person_code VARCHAR, player_name VARCHAR, status VARCHAR, note VARCHAR
        )
        """
    )
    con.execute("DELETE FROM player_overrides")
    if not path.exists():
        log.info("no overrides file at %s", path)
        return []

    applied: list[dict[str, Any]] = []
    for row in read_file(path):
        code = resolve(con, row["player"])
        found = con.execute("SELECT name FROM players WHERE person_code = ?", [code]).fetchone()
        if found is None:  # resolve() just matched it, so this cannot happen quietly
            raise OverrideError(f"{code} vanished between resolving and reading it")
        name = found[0]
        con.execute(
            "INSERT INTO player_overrides VALUES (?, ?, ?, ?)",
            [code, name, row["status"], row["note"]],
        )
        applied.append({"person_code": code, "player_name": name, **row})
        log.info("override: %s -> %s (%s)", name, row["status"], row["note"])
    return applied


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Apply manual player overrides")
    parser.add_argument("--db", default="data/euroleague.duckdb", type=Path)
    parser.add_argument("--file", default="data/overrides.csv", type=Path)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    con = duckdb.connect(str(args.db))
    try:
        applied = load(con, args.file)
    finally:
        con.close()
    excluded = [a for a in applied if a["status"] in EXCLUDING]
    print(f"{len(applied)} overrides applied, {len(excluded)} of them excluding")


if __name__ == "__main__":
    main()
