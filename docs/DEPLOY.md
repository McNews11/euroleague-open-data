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

## Host: HuggingFace Spaces

Chosen after checking what is actually still free. Fly.io and Railway no longer have free
tiers; Render's sleeps after 15 minutes of inactivity. HuggingFace Spaces gives 2 vCPU /
16 GB, unmetered CPU, a public HTTPS URL, and sleeps only after **48 hours** of inactivity.
Cold start after sleep is 30–90 seconds.

Ephemeral storage is not a problem here: the warehouse is baked into the image and is only
ever read.

### Steps

Steps 1 and 3 need your HuggingFace account, so they are yours. The rest is scripted.

1. **Create the Space.** Sign in at [huggingface.co](https://huggingface.co), then
   New Space → name `euroleague-open-data` → SDK **Docker** → visibility **Public**.

2. **Build the Space tree.** `scripts/publish_space.sh` generates `build/space` from the
   current source: it copies what the image needs, bakes the real hostname into the
   landing page, writes the HF README frontmatter, sets up LFS for the warehouse, and
   refuses to publish if the warehouse is missing or empty.

   ```bash
   scripts/publish_space.sh <your-hf-username>/euroleague-open-data
   ```

   The Space is a generated artefact rather than a branch, so refreshing it later is a
   rerun, never a merge conflict.

3. **Push.** Git will ask for your HF username and a token as the password — create one at
   [Settings → Access Tokens](https://huggingface.co/settings/tokens) with **write**
   permission.

   ```bash
   git -C build/space push --force origin main
   ```

4. **Set `PUBLIC_HOST`.** In the Space's Settings → Variables and secrets, add:

   ```
   PUBLIC_HOST = <your-hf-username>-euroleague-open-data.hf.space
   ```

   This is not optional, and getting it wrong is hard to diagnose: the server compares the
   `Host` header against an allow-list that starts empty, so every request comes back as a
   bare `421` that looks exactly like the Space being down. Accepts a comma-separated list
   if you later add a custom domain.

   If something still returns 421 in the wild, set `DISABLE_HOST_CHECK=1` to switch host
   validation off without a rebuild. It is a safe fallback here — the protection guards
   servers bound to loopback, and this one is public, read-only and unauthenticated.

5. **Verify from outside.** Once the build finishes:

   ```bash
   curl https://<your-hf-username>-euroleague-open-data.hf.space/health
   ```

   A healthy response lists the loaded seasons. It queries the warehouse rather than just
   returning 200, so a container that started without its data reports unhealthy.

### What has been verified locally

The image is build-tested, not merely written. Against the running container:

| Check | Result |
|---|---|
| `docker build` | succeeds, 915 MB |
| `/health` | `ok`, 1 123 games across 4 seasons |
| `/` landing page | serves the real 7 450-byte page, not the fallback stub |
| MCP handshake | protocol `2025-11-25`, 13 tools, 3 resources |
| `run_sql` guardrail | refuses `DROP TABLE` over HTTP |
| Host `…hf.space` | 200 |
| Host `evil.example.com` | 421 |
| Docker `HEALTHCHECK` | healthy |

The local build is arm64 and HuggingFace builds amd64 from the same Dockerfile, so the
build itself is re-run there; what is proven here is the Dockerfile's logic, not its
portability.

## Refreshing the data

The warehouse is a build artefact. To publish new data: crawl, rebuild, verify locally,
then push.

```bash
uv run euroleague-etl --competition E --season E2026     # crawl one season
uv run euroleague-etl --skip-crawl                        # rebuild everything from cache
uv run pytest
scripts/publish_space.sh <your-hf-username>/euroleague-open-data
git -C build/space push --force origin main
```

The crawl is slow on purpose — roughly 10 requests per minute, which is what the upstream
tolerates. A full season takes about two hours. Anything already cached costs nothing.

## Local check before deploying

```bash
EUROLEAGUE_DB=$PWD/data/euroleague.duckdb uv run euroleague-mcp-http
curl http://127.0.0.1:7860/health
```

Then point a client at `http://127.0.0.1:7860/mcp`.

## Cost

Zero, at the time of writing. The only paid element in the whole chain is a ChatGPT
subscription, which is your friends' side and unrelated to hosting. Free tiers change —
re-check before assuming.
