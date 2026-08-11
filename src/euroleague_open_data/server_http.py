"""Public HTTP deployment: the MCP endpoint and its landing page in one container.

ChatGPT cannot run a local stdio server. Its custom connectors require a remote server
reachable over HTTPS speaking Streamable HTTP or SSE, so a public deployment is the only
way to share this with someone who uses ChatGPT rather than Claude.

The MCP SDK exposes the transport as a Starlette app, which means the install
instructions can be served from the same process at `/` while the protocol lives at
`/mcp`. One deployment, one URL to hand out, nothing to keep in sync.

The server still reads only the bundled DuckDB file. Nothing here can reach upstream.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from .mcp_server import _con, mcp

log = logging.getLogger(__name__)

LANDING_PAGE = Path(__file__).resolve().parents[2] / "web" / "index.html"


def _public_host() -> str:
    """The hostname this deployment answers on.

    DNS-rebinding protection rejects requests whose Host header is not allow-listed, and
    the default allow-list is empty, so a deployment behind any real domain must be told
    its own name. Set PUBLIC_HOST at deploy time.
    """
    return os.environ.get("PUBLIC_HOST", "").strip()


async def landing(_request: Request) -> Response:
    if LANDING_PAGE.exists():
        return HTMLResponse(LANDING_PAGE.read_text())
    return HTMLResponse("<h1>euroleague-open-data</h1><p>MCP endpoint at <code>/mcp</code></p>")


async def health(_request: Request) -> Response:
    """Liveness plus a real answer about what is loaded.

    A health check that only returns 200 tells you the process is up, not that it can
    serve anything. This one queries the warehouse, so a container that booted without
    its data reports unhealthy instead of quietly serving an empty database.
    """
    try:
        con = _con()
        seasons = con.execute(
            "SELECT season_code, count(*) FROM games GROUP BY 1 ORDER BY 1"
        ).fetchall()
        games = con.execute("SELECT count(*) FROM games").fetchone()
    except Exception as exc:  # noqa: BLE001 - the point is to report any failure
        return JSONResponse({"status": "unhealthy", "error": str(exc)}, status_code=503)

    if not seasons:
        return JSONResponse({"status": "unhealthy", "error": "warehouse is empty"}, 503)

    return JSONResponse(
        {
            "status": "ok",
            "seasons": {code: count for code, count in seasons},
            "games": games[0] if games else 0,
            "transport": "streamable-http",
            "endpoint": "/mcp",
        }
    )


def build_app(host: str | None = None):
    """Starlette app serving the landing page, a health check and the MCP endpoint."""
    public = host if host is not None else _public_host()
    allowed = ["localhost", "127.0.0.1", "0.0.0.0"]
    if public:
        allowed.append(public)

    security = TransportSecuritySettings(
        # Only relax the check when no public host is configured, i.e. local development.
        enable_dns_rebinding_protection=bool(public),
        allowed_hosts=allowed,
        allowed_origins=[f"https://{public}"] if public else [],
    )

    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        # Stateless: every request carries its own context, so the container can be
        # restarted or replaced without breaking a client mid-session. The warehouse is
        # read-only, so there is no session state worth keeping anyway.
        stateless_http=True,
        json_response=False,
        transport_security=security,
    )
    app.routes.insert(0, Route("/", landing, methods=["GET"]))
    app.routes.insert(1, Route("/health", health, methods=["GET"]))
    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    port = int(os.environ.get("PORT", "7860"))  # 7860 is the HuggingFace Spaces default
    host = os.environ.get("BIND_HOST", "0.0.0.0")

    public = _public_host()
    log.info("serving MCP at /mcp, landing page at / (public host: %s)", public or "unset")
    if not public:
        log.warning(
            "PUBLIC_HOST is not set; DNS rebinding protection is disabled. "
            "Set it to the deployment's hostname before exposing this publicly."
        )

    uvicorn.run(build_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
