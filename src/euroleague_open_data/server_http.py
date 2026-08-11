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
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from .mcp_server import _con, mcp

log = logging.getLogger(__name__)

# What the checked-in landing page says before a running server rewrites it.
PLACEHOLDER_SCHEME = "https"
PLACEHOLDER_HOST = "YOUR-DEPLOYMENT"


def _landing_page() -> Path | None:
    """Locate the landing page, which sits outside the package.

    Resolving it relative to `__file__` works from a source checkout and breaks in a
    container, where the package is installed into site-packages and `web/` sits next to
    the working directory instead. Both layouts are tried, so the same code serves the
    real page either way rather than quietly falling back to the stub.
    """
    override = os.environ.get("LANDING_PAGE", "").strip()
    candidates = [
        *( [Path(override)] if override else [] ),
        Path.cwd() / "web" / "index.html",            # container: WORKDIR with web/ beside it
        Path(__file__).resolve().parents[2] / "web" / "index.html",  # source checkout
    ]
    return next((p for p in candidates if p.is_file()), None)


def _public_hosts() -> list[str]:
    """The hostnames this deployment answers on.

    Host validation rejects anything not allow-listed, and the default allow-list is
    empty, so a deployment behind a real domain must be told its own name. Accepts a
    comma-separated list so a Space can also be reached through a custom domain.
    """
    raw = os.environ.get("PUBLIC_HOST", "")
    hosts = [h.strip() for h in raw.split(",") if h.strip()]

    # Platforms that announce the hostname they assigned save the operator from having to
    # copy it into a variable by hand -- the step whose omission produces a blanket 421
    # that looks like an outage. Explicit config still wins.
    for var in ("RENDER_EXTERNAL_HOSTNAME", "KOYEB_PUBLIC_DOMAIN", "SPACE_HOST"):
        assigned = os.environ.get(var, "").strip()
        if assigned and assigned not in hosts:
            hosts.append(assigned)
    return hosts


async def landing(request: Request) -> Response:
    """The install instructions, with this deployment's own URL filled in.

    The page quotes the endpoint people have to paste into their client, so that URL has
    to be right. Baking it in at publish time means it is wrong for anyone who forks,
    moves host, or adds a domain -- and wrong in the quiet way, where the page still
    renders and the command it shows just does not work. The request knows the hostname,
    so the substitution happens here instead.
    """
    page = _landing_page()
    if page is None:
        return HTMLResponse("<h1>euroleague-open-data</h1><p>MCP endpoint at <code>/mcp</code></p>")

    host = request.headers.get("host", "")
    # Behind a proxy the connection is plain HTTP; the client's scheme is the forwarded one.
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    html = page.read_text()
    if host:
        html = html.replace(f"{PLACEHOLDER_SCHEME}://{PLACEHOLDER_HOST}", f"{scheme}://{host}")
    return HTMLResponse(html)


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


def _security_settings(public: list[str]) -> TransportSecuritySettings:
    """Host and Origin allow-lists for the transport.

    Two things about the SDK's matcher drive this. It compares the Host header
    *exactly*, and the header carries the port -- so a bare `127.0.0.1` never matches
    `127.0.0.1:7861`. Every name therefore needs its `:*` port wildcard alongside it.

    The check is also on unconditionally rather than only when a public host is set.
    Rebinding attacks target servers bound to a loopback or private address, so local
    development is where the protection actually earns its keep; leaving it off there and
    on in production had it exactly backwards.

    Absent Origin headers are allowed by the SDK, which is the normal case here: Claude
    and ChatGPT open these connections server-side, not from a page. Browser origins are
    still enumerated for anything that does send one, and DISABLE_HOST_CHECK exists as a
    one-variable escape hatch, because the failure mode is a bare 421 that looks like the
    server is simply down.
    """
    names = ["localhost", "127.0.0.1", "0.0.0.0", *public]
    hosts = [pattern for name in names for pattern in (name, f"{name}:*")]

    origins = [
        f"{scheme}://{name}"
        for name in names
        for scheme in ("https", "http")
    ]
    origins += [f"{o}:*" for o in origins]
    origins += [e.strip() for e in os.environ.get("ALLOWED_ORIGINS", "").split(",") if e.strip()]

    disabled = os.environ.get("DISABLE_HOST_CHECK", "").strip().lower() in {"1", "true", "yes"}
    if disabled:
        log.warning("DISABLE_HOST_CHECK is set; Host and Origin validation are off")

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=not disabled,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def build_app(host: str | list[str] | None = None) -> Starlette:
    """Starlette app serving the landing page, a health check and the MCP endpoint."""
    if host is None:
        public = _public_hosts()
    elif isinstance(host, str):
        public = [host] if host else []
    else:
        public = host

    security = _security_settings(public)

    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        # Stateless: every request carries its own context, so the container can be
        # restarted or replaced without breaking a client mid-session. The warehouse is
        # read-only, so there is no session state worth keeping anyway.
        stateless_http=True,
        # Plain JSON responses rather than an SSE stream per request. Streaming exists so
        # a server can push notifications or progress mid-call; this one never does --
        # every tool is a read-only query that answers once. Keeping SSE cost real
        # reliability: behind Render's proxy, the server closing each stream left the
        # pooled upstream connection in a state the router treated as dead, and roughly
        # half of all requests came back as a 404 that never reached the container.
        json_response=True,
        transport_security=security,
    )
    app.routes.insert(0, Route("/", landing, methods=["GET"]))
    app.routes.insert(1, Route("/health", health, methods=["GET"]))
    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    port = int(os.environ.get("PORT", "7860"))  # hosts override this; the image exposes 7860
    host = os.environ.get("BIND_HOST", "0.0.0.0")

    public = _public_hosts()
    log.info(
        "serving MCP at /mcp, landing page at / (public host: %s)",
        ", ".join(public) or "unset",
    )
    if not public:
        log.warning(
            "PUBLIC_HOST is not set. Requests arriving under the deployment's real "
            "hostname will be rejected with 421. Set it before exposing this publicly."
        )

    uvicorn.run(build_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
