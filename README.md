# euroleague-open-data

An open EuroLeague / EuroCup basketball data warehouse, and an MCP server that lets an
LLM query it in natural language.

> **Unofficial.** Not affiliated with, endorsed by, or approved by Euroleague Basketball.
> Data originates from Euroleague Basketball and is retrieved from publicly accessible
> endpoints. For research and educational use. See [DISCLAIMER.md](DISCLAIMER.md).

## Why this exists

The upstream EuroLeague API is undocumented, unversioned, and rate-limited at roughly
**10 requests per minute** by Cloudflare. That makes it unusable for interactive analysis:
three questions in a row from one user would black out everyone else for five minutes.

So this project inverts the problem. A slow, polite, resumable crawler pulls the data into
a local DuckDB warehouse once. Everything else — the MCP server, the Parquet exports —
reads that snapshot.

**The MCP server never contacts upstream.** It holds no HTTP client. No amount of traffic
to this project can generate load on Euroleague Basketball's infrastructure.

## Status

Four seasons loaded: EuroLeague E2024 and E2025, EuroCup U2024 and U2025.

| Component | State |
|---|---|
| Throttled crawler with permanent cache | working |
| DuckDB warehouse, 8 base tables | working |
| Validation suite, 8 reconciliation checks | working |
| Derived analytics (TS%, eFG%, usage, Four Factors, shot zones) | working |
| MCP server, stdio transport, 13 tools + 3 resources | working |
| HTTP transport + landing page + Dockerfile | working locally, not yet deployed |
| Full backfill (52 seasons, 12 122 games) | not started, ~50h of crawling |
| HTTP transport, hosting, dataset publishing | not started |

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
git clone <this repo> && cd euroleague-open-data
uv sync
```

Build the warehouse. The crawl is deliberately slow — about two hours for one season —
and it is safe to interrupt and rerun, because every response is cached permanently.

```bash
uv run euroleague-etl --season E2025
```

Already have the cache and only changed the schema? Skip the network entirely:

```bash
uv run euroleague-etl --season E2025 --skip-crawl
```

## Connect it to Claude

### Claude Code

```bash
claude mcp add euroleague --env EUROLEAGUE_DB=$PWD/data/euroleague.duckdb -- $PWD/.venv/bin/python -m euroleague_open_data.mcp_server
```

### Remote, for sharing with other people

A hosted deployment serves the same tools over HTTPS, so anyone can connect by URL with
nothing installed — and it is the **only** way to use this from ChatGPT, which cannot run
local MCP servers. See [docs/DEPLOY.md](docs/DEPLOY.md).

```bash
claude mcp add --transport http euroleague https://<your-deployment>/mcp
```

Note that ChatGPT custom connectors require a paid plan (Plus, Pro, Business, Enterprise
or Edu) with Developer mode enabled. Claude Code and Claude Desktop work on any plan.

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "euroleague": {
      "command": "/absolute/path/to/euroleague-open-data/.venv/bin/python",
      "args": ["-m", "euroleague_open_data.mcp_server"],
      "env": {
        "EUROLEAGUE_DB": "/absolute/path/to/euroleague-open-data/data/euroleague.duckdb"
      }
    }
  }
}
```

## Things to ask it

```
Who had the best true shooting percentage in EuroLeague last season, minimum 20 games?
Compare Vezenkov and Nwora on efficiency and usage.
Which team had the best defensive rating, and which Four Factor drove it?
Show me Micic's shot chart by zone.
Which games are unreliable for lineup analysis?
```

That last one matters. The warehouse tracks its own completeness, so the model can say
"this game has no shot data" instead of inventing a number.

## Fantasy drafting

Built for [BasketNews Fantasy](https://fantasy.basketnews.com/lt/rules) **draft mode**:
private leagues of 3–12 managers, 13-player rosters, unique squads, snake or reverse-snake
order.

Fantasy points are **recomputed exactly from boxscores**, not estimated. Every term in the
modern scoring system maps onto a stored field, so a player's score here is the score the
game would award.

Ranking is by **value over replacement**, not by average. In a draft each manager gets a
unique roster, so what decides a pick is how much better a player is than the next one
available at the same position — and that depends on league size. An 8-team league and a
12-team league produce different boards from the same data.

```
uv run python -m euroleague_open_data.fantasy --teams 8 --scoring classic
```

Two things about scoring are worth knowing before you trust a late-round pick:

1. **The "classic" formula is not published for draft mode.** The rules page describes it
   only as "traditional scoring based on player statistics". Budget mode gives an explicit
   formula — PIR ±10% by team result — and that is what `classic` implements here. It is an
   assumption, and it is marked as one in `fantasy.py`.
2. **It matters less than it looks.** Rank correlation between classic and modern is 0.99
   and seven of the top eight are the same players. The disagreement shows up in the
   middle: one player moves 94th to 61st, which in an 8×13 draft is a different round.

## Tools

| Tool | Purpose |
|---|---|
| `search_players` | fuzzy name → canonical `person_code` |
| `search_teams` | fuzzy name → canonical `team_code`, handles sponsor renames |
| `get_player_stats` | season or career, totals / per-game / per-36, plus TS%, eFG%, usage |
| `get_team_stats` | ratings and Four Factors, team and opponent |
| `get_game_boxscore` | full game detail with completeness flags |
| `get_shot_chart` | zone aggregates, optionally raw x/y coordinates |
| `run_sql` | read-only DuckDB SELECT — the escape hatch for unanticipated questions |
| `get_draft_board` | draft ranking by value over replacement, sized to your league |
| `plan_snake_draft` | your picks in snake order, and who should survive until each |
| `compare_draft_candidates` | head to head for a specific pick decision |
| `get_player_fantasy_log` | game-by-game fantasy points, for form and role changes |
| `get_coach_rotation` | how deep a coach's rotation runs — the ceiling on minutes |
| `get_role_outlook` | minutes and production a club vacated, by position |

Resources: `euroleague://schema`, `euroleague://coverage`, `euroleague://data-quality`.

`run_sql` runs on a read-only connection, permits a single `SELECT`/`WITH`, caps rows, and
cancels after 15 seconds.

## Data quality

Validation runs as part of every ETL run and writes
[`docs/data-quality-report.json`](docs/data-quality-report.json), which is committed so
regressions show up in `git log`.

Three findings worth knowing about, all documented in
[`docs/api-notes.md`](docs/api-notes.md):

1. **Shot coordinates and play-by-play begin at the 2007 season.** Earlier seasons have
   boxscores only. This is a property of the source, not of this project.
2. **Period buckets and event sequence numbers disagree in roughly 40% of games.**
   `NUMBEROFPLAY` is unique and reliable; the per-quarter arrays upstream returns are not.
   Affected games are flagged `lineup_safe = false`.
3. **Player identifiers differ across endpoints.** Boxscores use `TGB`, the live feed uses
   `PTGB`. Normalisation is source-aware, and there is a regression test for it.

## Coverage

Measured across all 52 seasons on 2026-08-10 (`docs/coverage.json`):

| Segment | Seasons | Games | Boxscore | PBP + shots |
|---|---|---|---|---|
| EuroLeague | E2000–E2006 | 1 563 | yes | no |
| EuroCup | U2002–U2006 | 896 | yes | no |
| Both | 2007–2025 | 9 059 | yes | yes |
| **Total** | **52** | **12 122** | | |

## Development

```bash
uv run pytest
uv run ruff check src tests
uv run mypy src
```

## Licence and contact

Code is MIT — see [LICENSE](LICENSE). **The licence covers the code only.** It grants no
rights in the underlying match data, which belongs to Euroleague Basketball and its data
partners. Commercial use of the data may require a licence from them.

**Takedown:** if you represent a rights holder and want this changed or removed, open a
GitHub issue titled `TAKEDOWN`. We will respond within 7 days and will take published
datasets down on request while any disagreement is discussed. No formal legal process is
needed to get our attention.

## Prior art

- [`giasemidis/euroleague_api`](https://github.com/giasemidis/euroleague_api) — Python
  wrapper. The shot-coordinate endpoint used here was reverse-engineered there first.
- [`FlavioLeccese92/euroleaguer`](https://github.com/FlavioLeccese92/euroleaguer) — R
  wrapper, useful for cross-checking endpoint coverage.
- [`bsamot10/EuroleagueDataETL`](https://github.com/bsamot10/EuroleagueDataETL) — existing
  ETL patterns for this data.
- [`vtzimpl/euroleague-api-mcp`](https://github.com/vtzimpl/euroleague-api-mcp) — an
  earlier MCP server that proxies the API directly. Given the rate limit measured here,
  proxying is the thing this project deliberately avoids.
