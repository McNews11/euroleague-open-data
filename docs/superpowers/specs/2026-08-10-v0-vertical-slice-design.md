# V0 — vertical slice design

**Date:** 2026-08-10
**Status:** approved, in build

## Goal

Prove the entire chain end to end on one season, so we learn whether the result is worth
scaling to 12 122 games *before* spending ~50 hours of throttled crawling on it.

This is a slice, not a prototype. Everything built here is production code for the full
project; only the data range is narrow.

## Scope

**In:** EuroLeague season `E2025` (2025-26). 402 games — the largest season on record, and
the one the acceptance question refers to as "last season".

**Out of V0, deliberately:** EuroCup, seasons before 2025, lineup reconstruction, HTTP
transport, Docker, hosting, GitHub Actions, dataset publishing. Each of those is only
worth building once the core is known to be good.

## Architecture

```
api-live v2 + live.euroleague.net
      │   throttled client: ~10 req/min, Retry-After, permanent disk cache
      ▼
  raw JSON on disk  ──▶  validation  ──▶  DuckDB  ──▶  Parquet / CSV
                                            │
                                            ▼
                                    MCP stdio server
```

**The invariant:** the MCP server reads the warehouse and nothing else. It holds no HTTP
client. This is enforced structurally — the server module does not import the fetch layer.

### Why the raw cache is a separate stage

Fetching and parsing are separated so that a schema mistake costs a re-parse, not a
re-crawl. At 10 req/min a re-crawl of E2025 costs two hours; a re-parse costs seconds.
Raw responses are written verbatim, before any interpretation.

## Request budget

| Call | Count |
|---|---|
| v2 `seasons`, `clubs`, `people`, `games` | 4 |
| v2 `games/{n}/stats` | 402 |
| `PlayByPlay` | 402 |
| `Points` | 402 |
| **Total** | **~1 210 → ≈ 2 h** |

`Header` is skipped; the season-wide v2 `games` call carries the same fields in one
request rather than 402. See `docs/api-notes.md` §3.

## Data model

`seasons`, `teams`, `players`, `games`, `boxscores_player`, `boxscores_team`, `shots`,
`play_by_play`.

Plus `player_id_crosswalk`, which is not optional — the three sources use three different
identifier formats for the same person (`docs/api-notes.md` §5.2).

Every table carries a completeness indicator so the MCP layer can say "lineup data is
unavailable for this game" instead of inventing a number.

## Validation

Runs as part of the ETL and fails loudly rather than ingesting silently.

1. Boxscore player totals reconcile against the team total.
2. Team totals reconcile against the final score in `games`.
3. Play-by-play scoring events reconcile against the boxscore.
4. Shot rows reconcile against boxscore field-goal attempts.
5. Events with non-monotonic `NUMBEROFPLAY` are quarantined and logged individually.

Output is a per-run data-quality report committed to the repo, so regressions show up in
`git log` rather than being discovered by a user.

A `200` response is never treated as proof of data — `U2013` and `U2016` return `200` with
empty shot arrays.

## Derived analytics

Materialised as DuckDB tables, not computed per query: `TS%`, `eFG%`, usage rate.

Lineup reconstruction is explicitly deferred. It is the hardest component, it depends on
`NUMBEROFPLAY` ordering rather than a clock, and it must not block first feedback.

## MCP surface

`search_players`, `search_teams`, `get_player_stats`, `get_game_boxscore`,
`get_shot_chart`, `run_sql`.

`run_sql` is read-only with a hard row limit and a statement timeout. The schema DDL is
exposed as an MCP resource so the model can read table definitions before writing SQL.

## Acceptance

Connect over stdio to Claude Code and ask:

> "Who had the best true shooting percentage in EuroLeague last season, minimum 20 games?"

The answer must be correct and must be hand-verifiable against DuckDB. Anything less means
V0 failed, regardless of how much of it was built.

## Legal posture

MIT on code only. `DISCLAIMER.md` written before any data was ingested, not retrofitted.
Euroleague Basketball credited as data origin on every export. Takedown path documented.
