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

These need your accounts, so they are yours to do. Nothing here can be automated on your
behalf.

1. **Create the Space.** Sign in at [huggingface.co](https://huggingface.co), then
   New Space → SDK **Docker** → visibility **Public**.

2. **Point it at this repo, or push directly.** The Space is a git repo:

   ```bash
   git remote add space https://huggingface.co/spaces/<you>/euroleague-open-data
   git push space main
   ```

3. **The warehouse must be in the pushed repo.** It is ~23 MB and gitignored by default.
   Track it with LFS for the Space:

   ```bash
   git lfs install
   git lfs track "data/euroleague.duckdb"
   git add -f data/euroleague.duckdb .gitattributes
   git commit -m "Ship the warehouse with the image"
   ```

4. **Set the Space metadata.** HuggingFace reads YAML frontmatter from `README.md`. Add
   this to the top of the README **on the Space branch only** — it is Space-specific and
   does not belong in the GitHub README:

   ```yaml
   ---
   title: EuroLeague Open Data
   emoji: 🏀
   colorFrom: gray
   colorTo: orange
   sdk: docker
   app_port: 7860
   pinned: false
   ---
   ```

5. **Set `PUBLIC_HOST`.** In the Space's Settings → Variables, add:

   ```
   PUBLIC_HOST = <you>-euroleague-open-data.hf.space
   ```

   This is not optional. DNS-rebinding protection rejects requests whose `Host` header is
   not allow-listed, and the allow-list starts empty. Without it the server refuses every
   request from the real domain.

6. **Verify from outside.** Once the build finishes:

   ```bash
   curl https://<you>-euroleague-open-data.hf.space/health
   ```

   A healthy response lists the loaded seasons. It queries the warehouse rather than just
   returning 200, so a container that started without its data reports unhealthy.

7. **Update the landing page URL.** Replace `YOUR-DEPLOYMENT` in `web/index.html` with the
   real hostname, then push again.

## Refreshing the data

The warehouse is a build artefact. To publish new data: crawl, rebuild, verify locally,
then push.

```bash
uv run euroleague-etl --competition E --season E2026     # crawl one season
uv run euroleague-etl --skip-crawl                        # rebuild everything from cache
uv run pytest
git add -f data/euroleague.duckdb && git commit -m "Refresh warehouse" && git push space main
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
