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

## Host: Koyeb

Picked after the previous choice stopped being free. **HuggingFace Spaces no longer works
for this**: as of August 2026 Docker and Gradio Spaces require a PRO subscription, and
only Static Spaces — which cannot run Python — remain free. Fly.io and Railway have no
free tier either. Render's free tier is real but sleeps after 15 minutes idle.

Koyeb gives one always-on free service: 0.1 vCPU, 512 MB RAM, no sleep, public HTTPS,
built from a Dockerfile in a GitHub repo. It may ask for a card if it cannot verify you
are human; nothing is charged on the free instance.

Treat every claim in this paragraph as perishable. The last host was free when this file
was first written and was not free a week later.

### Steps

1. **Sign up** at [koyeb.com](https://app.koyeb.com). "Continue with GitHub" is the
   shortest path and grants the repo access Koyeb needs to build.

2. **Create the service.** Create Web Service → GitHub → `euroleague-open-data`, branch
   `main`. Koyeb detects the `Dockerfile` on its own.

3. **Instance type: Free.** It is not the default. Check before deploying.

4. **Port 7860.** The container listens there and the healthcheck path is `/health`.

5. **Set `PUBLIC_HOST`** in the service's environment variables, to the hostname Koyeb
   assigns (`<service>-<org>.koyeb.app`).

   This is not optional, and getting it wrong is hard to diagnose: the server compares the
   `Host` header against an allow-list that starts empty, so every request comes back as a
   bare `421` that looks exactly like the service being down. Accepts a comma-separated
   list if you later add a custom domain.

   If something still returns 421 in the wild, set `DISABLE_HOST_CHECK=1` to switch host
   validation off without a rebuild. It is a safe fallback here — the protection guards
   servers bound to loopback, and this one is public, read-only and unauthenticated.

6. **Verify from outside.**

   ```bash
   curl https://<your-service>.koyeb.app/health
   ```

   A healthy response lists the loaded seasons. It queries the warehouse rather than just
   returning 200, so a container that started without its data reports unhealthy.

The landing page does **not** need updating with the deployment URL. It reads the hostname
off the request and fills itself in, so a fork or a new domain gets correct instructions
with no edit.

### Memory

The free instance has 512 MB and an OOM kill takes down the container for everyone, not
just the request that caused it. DuckDB is therefore capped at `DUCKDB_MEMORY_LIMIT`
(default 256 MB) so a heavy `run_sql` spills to disk or fails alone. Raise it on a larger
instance.

## What has been verified locally

The image is build-tested, not merely written. Against the running container:

| Check | Result |
|---|---|
| `docker build` | succeeds, 915 MB |
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
publish new data: crawl, rebuild, verify, push. Koyeb redeploys on push.

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
this document has already been rewritten once for exactly that reason.
