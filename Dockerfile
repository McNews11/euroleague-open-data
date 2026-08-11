# Public MCP deployment. Serves the landing page at / and the MCP endpoint at /mcp.
#
# The warehouse is baked into the image rather than fetched at boot. It is ~23 MB, it is
# read-only, and a container that starts without its data is useless -- so failing at
# build time is better than starting healthy and answering nothing.

FROM python:3.12-slim

# Several hosts run containers as a non-root uid and will not grant root.
RUN useradd -m -u 1000 app
WORKDIR /app

# Dependencies first, so a code change does not reinstall the world.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .

COPY web ./web
COPY data/euroleague.duckdb ./data/euroleague.duckdb

RUN chown -R app:app /app
USER app

ENV EUROLEAGUE_DB=/app/data/euroleague.duckdb \
    PORT=7860 \
    BIND_HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1

EXPOSE 7860

# Fails if the warehouse cannot be queried, not merely if the process is alive.
HEALTHCHECK --interval=60s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys,json; \
r=urllib.request.urlopen('http://127.0.0.1:7860/health',timeout=8); \
sys.exit(0 if json.load(r).get('status')=='ok' else 1)"

CMD ["python", "-m", "euroleague_open_data.server_http"]
