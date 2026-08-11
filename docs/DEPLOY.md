# Deploying the public server

The MCP endpoint and its landing page run in one container. Anyone you give the URL to can
connect without installing anything.

**Why hosting is needed at all:** ChatGPT cannot run local MCP servers. Its custom
connectors require a remote server reachable over HTTPS speaking Streamable HTTP or SSE.
Claude Code and Claude Desktop have no such restriction — for Claude alone, a local stdio
install is enough and costs nothing.

## What your friends will need

| Client | Works on free plan? | Notes |
|---|---|---|
| Claude Code | yes | one command, no account |
| Claude Desktop | yes | paste the URL as a custom connector |
| **ChatGPT** | **no** | custom connectors need Plus, Pro, Business, Enterprise or Edu, plus Developer mode enabled in Settings → Apps → Advanced |

Say this up front to anyone you invite. A friend on free ChatGPT cannot use this, and that
is OpenAI's restriction, not ours.

## Host: Render

Third choice, because the first two stopped being free while this was being built.

- **HuggingFace Spaces** — Docker and Gradio Spaces now require PRO. Only Static Spaces,
  which cannot run Python, remain free.
- **Koyeb** — acquired by Mistral in February 2026. New accounts can register on paid
  plans only; the console shows no deploy UI at all on a free signup. Pro starts at $29/mo.
- **Fly.io, Railway** — no free tier.

Render's free tier is real, needs no credit card, and builds from a Dockerfile in a GitHub
repo. The cost is that a free web service **spins down after 15 minutes without traffic**,
so the first request after an idle period waits for a cold start. 750 free instance hours
per workspace per month.

For a draft tool this is livable: the first question of a session is slow, everything
after it is not. It is the one thing to warn people about before they think it is broken.

Treat every claim here as perishable. This section has been rewritten twice already.

### Steps

1. **Sign up** at [render.com](https://render.com) — "Continue with GitHub" is shortest
   and grants the repo access Render needs to build.

2. **New → Blueprint**, connect `McNews11/euroleague-open-data`. Render reads
   `render.yaml` and configures the service itself: Docker runtime, **free** instance
   type, `/health` as the health check, and the DuckDB memory cap.

   Doing it through New → Web Service instead works too, but then the instance type
   defaults to a paid plan and the health check path is blank — both easy to miss.

6. **Nothing to set for the hostname.** Render injects
   `RENDER_EXTERNAL_HOSTNAME` and the server reads it, so the allow-list configures
   itself. This used to be a manual `PUBLIC_HOST` step, and forgetting it returned a bare
   `421` on every request that looked exactly like the service being down. Set
   `PUBLIC_HOST` only to add a custom domain; it accepts a comma-separated list.

   If something still returns 421 in the wild, set `DISABLE_HOST_CHECK=1` to switch host
   validation off without a rebuild. It is a safe fallback here — the protection guards
   servers bound to loopback, and this one is public, read-only and unauthenticated.

7. **Verify from outside.**

   ```bash
   curl https://<your-service>.onrender.com/health
   ```

   A healthy response lists the loaded seasons. It queries the warehouse rather than just
   returning 200, so a container that started without its data reports unhealthy.

The landing page does **not** need updating with the deployment URL. It reads the hostname
off the request and fills itself in, so a fork or a new domain gets correct instructions
with no edit.

### Image size

Serving and crawling have separate dependencies. The image installs `.` only, so polars,
pyarrow and httpx — needed to *build* a warehouse, never to read one — stay out of it.
That took the image from 915 MB to 444 MB, which is time off every cold start on a tier
that cold-starts often. Use `uv sync --extra etl` when you actually want to crawl.

### Memory

The free instance has 512 MB and an OOM kill takes down the container for everyone, not
just the request that caused it. DuckDB is therefore capped at `DUCKDB_MEMORY_LIMIT`
(default 256 MB) so a heavy `run_sql` spills to disk or fails alone. Raise it on a larger
instance.

## What has been verified locally

The image is build-tested, not merely written. Against the running container:

| Check | Result |
|---|---|
| `docker build` | succeeds, 444 MB |
| `/health` | `ok`, 1 123 games across 4 seasons |
| `/` landing page | serves the real page with the request's own hostname substituted |
| MCP handshake | protocol `2025-11-25`, 13 tools, 3 resources |
| `run_sql` guardrail | refuses `DROP TABLE` over HTTP |
| Host `evil.example.com` | 421 |
| Docker `HEALTHCHECK` | healthy |

The local build is arm64 and the host builds amd64 from the same Dockerfile, so the build
itself is re-run there; what is proven here is the Dockerfile's logic, not its portability.

## Refreshing the data

The warehouse is committed to the repo because the host builds the image from it. To
publish new data: crawl, rebuild, verify, push. Render redeploys on push.

```bash
uv run euroleague-etl --competition E --season E2026     # crawl one season
uv run euroleague-etl --skip-crawl                        # rebuild everything from cache
uv run pytest
git add -f data/euroleague.duckdb
git commit -m "Refresh warehouse" && git push
```

The crawl is slow on purpose — roughly 10 requests per minute, which is what the upstream
tolerates. A full season takes about two hours. Anything already cached costs nothing.

Committing a 23 MB binary means every refresh adds another blob to history. If that
becomes a problem, move the file to a GitHub Release and fetch it in the Dockerfile.

## Local check before deploying

```bash
docker build -t euroleague-mcp . && docker run --rm -p 7861:7860 \
  -e PUBLIC_HOST=localhost euroleague-mcp
```

```bash
curl http://127.0.0.1:7861/health
```

Then point a client at `http://127.0.0.1:7861/mcp`.

## Cost

Zero, at the time of writing. The only paid element in the whole chain is a ChatGPT
subscription, which is your friends' side and unrelated to hosting. Free tiers change —
this document has already been rewritten twice for exactly that reason.
