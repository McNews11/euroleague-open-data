# Upstream API notes

Everything here was verified by live request on **2026-08-10**. Nothing in this document
is repeated from memory or from third-party documentation. Where a claim is not verified,
it says so.

Treat this file as perishable. The upstream is undocumented and unversioned in practice.

---

## 1. The OpenAPI spec is gated — do not plan around it

The Swagger UI at `https://api-live.euroleague.net/swagger/index.html` loads and
references three specs:

```
/swagger/v1/swagger.json
/swagger/v2/swagger.json
/swagger/v3/swagger.json
```

All three return **HTTP 200** and all three are **448 bytes** with an empty path set:

```json
{
  "openapi": "3.0.1",
  "info": { "title": "Competition Engine", "version": "2.0" },
  "servers": [{ "url": "https://api-live.euroleague.net" }],
  "paths": { },
  "components": {
    "securitySchemes": {
      "ApiKey": { "type": "apiKey", "name": "API Key", "in": "header" }
    }
  },
  "security": [{ "ApiKey": [] }]
}
```

The page's request interceptor confirms the intent — it attaches an `Authorization`
header from `localStorage` to `/swagger` requests. **Without an API key the spec exposes
no endpoints at all.** There is no route list to derive, save, or diff.

Consequence: the endpoint inventory below is empirical. It was built by probing candidate
routes and cross-checking against the open-source wrappers listed in the README.

---

## 2. Three hosts, not one

| Host | Serves | Format |
|---|---|---|
| `api-live.euroleague.net` | competition structure, clubs, people, games, boxscore stats | JSON (v2, v3), XML (v1) |
| `live.euroleague.net` | per-game play-by-play, shot coordinates, quarter scores | JSON |
| `feeds.incrowdsports.com` | mirror of v2 game stats | JSON |

`feeds.incrowdsports.com/provider/euroleague-feeds/v2/competitions/E/seasons/E2024/games/1/stats`
returned a byte-identical payload to the `api-live` equivalent (46 929 bytes both). It is
a CDN mirror, not an independent source. **We use `api-live` and treat the feed as a
fallback only.**

---

## 3. Verified endpoints

`{comp}` is `E` (EuroLeague) or `U` (EuroCup). `{season}` is `E2025`, `U2024`, etc.

### api-live v2 — JSON, primary source

| Route | Verified | Returns |
|---|---|---|
| `/v2/competitions/{comp}/seasons` | 200 | `{data[], total}` — 27 seasons for `E`, 25 for `U` |
| `/v2/competitions/{comp}/seasons/{season}/clubs` | 200 | 18 clubs for E2024, with `clubPermanentName`, country, crest URL |
| `/v2/competitions/{comp}/seasons/{season}/games` | 200 | `total` = season game count; per-game phase, round, group |
| `/v2/competitions/{comp}/seasons/{season}/games/{gameCode}` | 200 | single game header |
| `/v2/competitions/{comp}/seasons/{season}/games/{gameCode}/stats` | 200 | `{local, road}` → `players[]`, `team`, `total`, `coach` |
| `/v2/competitions/{comp}/seasons/{season}/people` | 200 | 883 records for E2024 — **includes coaches and referees**, must be filtered by `type` |
| `/v2/competitions/{comp}/seasons/{season}/rounds` | 200 | round list |
| `/v2/competitions/{comp}/seasons/{season}/rounds/{n}/standings` | 200 | standings for that round |

**`/v2/.../standings` without a round is 404.** Standings are round-scoped only. To build a
`standings` table you iterate rounds.

### api-live v3 — statistics only

| Route | Verified | Note |
|---|---|---|
| `/v3/competitions/{comp}/statistics/players/traditional` | 200 | query params are **PascalCase**: `SeasonMode`, `SeasonCode` |
| `/v3/competitions/{comp}/seasons` | **400** | v3 does not serve competition structure |
| `/v3/competitions/{comp}/seasons/{season}/games` | **405** | wrong verb — v3 is not a superset of v2 |

v3 is a narrow statistics surface bolted onto the same host. It is **not** "v2 but newer".
Do not assume a v2 route exists in v3.

Note the casing trap: the same query key is `seasonCode` on v2 and `SeasonCode` on v3.
Lowercase on v3 returns **400**.

### live.euroleague.net — per-game detail

| Route | Verified | Returns |
|---|---|---|
| `/api/PlayByPlay?gamecode={n}&seasoncode={season}` | 200 | events bucketed per period |
| `/api/Points?gamecode={n}&seasoncode={season}` | 200 | **shot coordinates** |
| `/api/Boxscore?gamecode={n}&seasoncode={season}` | 200 | `ByQuarter`, `EndOfQuarter`, `Stats`, `Referees`, `Attendance` |
| `/api/Header?gamecode={n}&seasoncode={season}` | 200 | venue, capacity, coaches, quarter scores, final score |

`Header` is largely redundant — the season-wide v2 `games` call carries the same fields in
one request instead of one per game. **Prefer v2 `games`; skip `Header` in bulk crawls.**

### api-live v1 — XML, legacy

`/v1/results`, `/v1/games`, `/v1/standings` all return 200 with
`application/xml`. Retained as a cross-check for old seasons; not used as a primary source.

---

## 4. Payload shapes

### `Points` (shots) — the reverse-engineered one

```json
{
  "NUM_ANOT": 51, "TEAM": "PAN       ", "ID_PLAYER": "P002328   ",
  "PLAYER": "GRIGONIS, MARIUS", "ID_ACTION": "2FGA",
  "ACTION": "Missed Two Pointer", "POINTS": 0,
  "COORD_X": -370, "COORD_Y": 156, "ZONE": "D",
  "FASTBREAK": "0", "SECOND_CHANCE": "0", "POINTS_OFF_TURNOVER": "0",
  "MINUTE": 1, "CONSOLE": "09:49",
  "POINTS_A": 0, "POINTS_B": 0, "UTC": "20241003164643"
}
```

Wrapped in `{"Rows": [...]}`. This endpoint is not in any published spec.

Note `FASTBREAK`, `SECOND_CHANCE`, `POINTS_OFF_TURNOVER` are **strings** `"0"`/`"1"`, not
booleans or ints. `UTC` is a packed `YYYYMMDDHHMMSS` string, not ISO 8601.

### `PlayByPlay`

```json
{
  "Live": false, "TeamA": "...", "CodeTeamA": "...",
  "FirstQuarter": [...], "SecondQuarter": [...],
  "ThirdQuarter": [...], "ForthQuarter": [...], "ExtraTime": [...]
}
```

Note the misspelling **`ForthQuarter`**. Events are not one flat array — they are five
separate keys, and `ExtraTime` holds *all* overtime periods together with no period
discriminator inside.

Event shape:

```json
{
  "TYPE": 0, "NUMBEROFPLAY": 49, "CODETEAM": "          ",
  "PLAYER_ID": "          ", "PLAYTYPE": "BP", "PLAYER": null,
  "TEAM": null, "DORSAL": null, "MINUTE": 1, "MARKERTIME": "",
  "POINTS_A": null, "POINTS_B": null, "COMMENT": "", "PLAYINFO": "Begin Period"
}
```

### `games/{n}/stats`

```
{local, road} → each has {coach, players[12], team{24 fields}, total{24 fields}}
```

---

## 5. Data-quality hazards found during recon

### 5.1 There is no absolute game clock in play-by-play

Events carry `MINUTE` (integer) plus `MARKERTIME` (string, **frequently empty**). There is
no monotonic timestamp. Ordering must come from **`NUMBEROFPLAY`**, which is sequential
across the whole game (period 2 starts at 176 in the sample, continuing from period 1).

This makes the "out-of-order events / backwards timestamps" warnings from prior art in
this space entirely credible. Lineup reconstruction must key on `NUMBEROFPLAY`, never on
the clock.

### 5.2 Player identifiers are inconsistent across endpoints

| Source | Format | Example |
|---|---|---|
| `Points` | space-padded, `P`-prefixed | `"P002328   "` |
| `PlayByPlay` | space-padded `PLAYER_ID` | `"          "` when absent |
| v2 `people` | bare numeric string | `"010179"` |

Team codes are padded the same way (`"PAN       "`). **Every identifier must be stripped
before use**, and a crosswalk table is required infrastructure, not an optimisation.

### 5.3 Empty-but-200 responses

Two seasons return `200` with no usable shot data:

- `U2013` → `Rows` present but **empty**
- `U2016` → no `Rows`

A `200` does not mean data exists. Validation must check content, not status.

---

## 6. Rate limiting — the single most important operational fact

The upstream sits behind **Cloudflare**, which rate-limits at the edge:

```
HTTP/2 429
retry-after: 243
server: cloudflare
content-length: 17

error code: 1015
```

Measured behaviour:

- **3 req/s got blocked after roughly 30 requests.**
- **0.67 req/s still got blocked twice**, after roughly 50 requests each time.
- Cooldown is **~300 s**.
- The limit applies to `api-live.euroleague.net` and `live.euroleague.net`
  **simultaneously** — being blocked on one blocks the other.
- The window is **rolling and self-healing, not escalating**. `retry-after` was observed
  counting down 243 → 117 while requests were still being attempted. Continued probing
  during a block did not extend the ban.

**Sustainable rate is therefore ≈ 10 requests/minute.** The exact quota is not published
and was not pinned down precisely; the crawler self-tunes rather than hard-coding a value.

Honouring `Retry-After` works. A sweep that slept 304 s and 305 s on two separate blocks
resumed cleanly with zero lost seasons.

### Why this dictates the architecture

At 10 req/min a full backfill of both competitions is:

```
 9 059 games (2007+, full detail) × 3 requests = 27 177
 2 459 games (pre-2007, boxscore only) × 1 request =  2 459
                                          total ≈ 30 000 requests
                                          ÷ 10/min ≈ 50 hours
```

This is why **the MCP server must never call upstream at request time**. A server that
proxied live would let a single user asking three questions in a row exhaust the quota and
black out every other user for five minutes.

It is also why the raw cache is permanent and why finished games are never refetched.

Operational note: **GitHub Actions jobs are capped at 6 hours**, so the initial backfill
cannot run in CI. Cron is suitable for incremental updates only.

---

## 7. Coverage — measured, not assumed

Full sweep of all 52 seasons (27 EuroLeague, 25 EuroCup) on 2026-08-10. `games` is the season total reported by v2;
`shots` is the row count for game 1 of that season.

**Shot coordinates and play-by-play begin exactly at 2007**, in both competitions.
`E2006` → no rows. `E2007` → 175 rows.

| Segment | Seasons | Games | Boxscore | PBP + shots |
|---|---|---|---|---|
| EuroLeague | E2000–E2006 | 1 563 | yes | **no** |
| EuroCup | U2002–U2006 | 896 | yes | **no** |
| Both | 2007–2025 | 9 059 | yes | yes |
| Both, unplayed | E2026, U2026 | 604 | schedule only | — |
| **Total** | **52** | **12 122** | | |

Per-season detail is in `docs/coverage.json`.

---

## 8. Things deliberately not verified

Stated so nobody mistakes silence for confirmation:

- Whether the Cloudflare quota is per-IP, per-ASN, or global.
- Whether an API key is obtainable, and on what terms.
- Whether `v1` XML covers seasons that `v2` omits.
- Whether `feeds.incrowdsports.com` has a separate rate-limit budget.
- Kaggle seed dataset contents — the page is JavaScript-rendered and downloads require an
  authenticated Kaggle account.


## 9. Announced rosters for an unplayed season

Found 2026-08-11, and it contradicts the obvious reading of the API.

`/v2/competitions/E/seasons/E2026/people` returns `total: 0`, which looks like "rosters are
not published yet". They are. `/v2/competitions/E/seasons/E2026/clubs/{code}/people`
returns full squads for all 20 clubs -- 301 players, 270 of them with person codes.

The season-wide aggregate is empty while the per-club route is populated. Taking the
aggregate at face value means reporting an absence of data as a fact about the world, which
is the recurring failure mode in this project.

Two shapes differ from the rest of the API:

- the per-club route returns a bare JSON list, not the usual `{"data": [...]}` envelope;
- `person.code` is `null` for players new to the competition (31 of 301 in E2026), so they
  cannot be joined to history at all.

Entries carry `type` -- `"J"` is a player; coaches and staff share the endpoint.
