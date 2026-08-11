"""Host validation for the public deployment.

A wrong allow-list here does not fail loudly. Every request returns a bare 421 and the
server looks like it is down, which is an expensive thing to debug through a remote
container's build queue. So the matcher is tested against the header values real
deployments actually send, port included.
"""

from __future__ import annotations

import pytest
from mcp.server.transport_security import TransportSecurityMiddleware

from euroleague_open_data.server_http import _security_settings

SPACE = "euroleague-open-data.koyeb.app"


def _matcher(public: list[str], monkeypatch: pytest.MonkeyPatch) -> TransportSecurityMiddleware:
    monkeypatch.delenv("DISABLE_HOST_CHECK", raising=False)
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
    return TransportSecurityMiddleware(_security_settings(public))


@pytest.mark.parametrize(
    "host",
    [
        SPACE,          # what the platform forwards in production
        f"{SPACE}:443",  # ...and the same name carrying an explicit port
        "localhost",
        "localhost:7860",
        "127.0.0.1:7861",  # the local container check, which a bare entry does not match
    ],
)
def test_legitimate_hosts_are_accepted(host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _matcher([SPACE], monkeypatch)._validate_host(host)


@pytest.mark.parametrize("host", ["evil.example.com", "evil.example.com:443", "", None])
def test_foreign_hosts_are_rejected(host: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    assert not _matcher([SPACE], monkeypatch)._validate_host(host)


def test_absent_origin_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Claude and ChatGPT open these connections server-side and send no Origin."""
    assert _matcher([SPACE], monkeypatch)._validate_origin(None)


def test_own_origin_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _matcher([SPACE], monkeypatch)._validate_origin(f"https://{SPACE}")


def test_multiple_public_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment reachable through a custom domain as well as its platform name."""
    monkeypatch.setenv("PUBLIC_HOST", f"{SPACE}, euroleague.example.org")
    from euroleague_open_data.server_http import _public_hosts

    hosts = _public_hosts()
    assert hosts == [SPACE, "euroleague.example.org"]

    matcher = _matcher(hosts, monkeypatch)
    assert matcher._validate_host("euroleague.example.org")
    assert matcher._validate_host(SPACE)


def test_platform_assigned_hostname_is_picked_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """Render announces its own hostname; not using it is a self-inflicted 421."""
    monkeypatch.delenv("PUBLIC_HOST", raising=False)
    monkeypatch.setenv("RENDER_EXTERNAL_HOSTNAME", "euroleague.onrender.com")
    from euroleague_open_data.server_http import _public_hosts

    assert _public_hosts() == ["euroleague.onrender.com"]
    assert _matcher(_public_hosts(), monkeypatch)._validate_host("euroleague.onrender.com")


def test_explicit_config_and_platform_hostname_coexist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBLIC_HOST", "custom.example.org")
    monkeypatch.setenv("RENDER_EXTERNAL_HOSTNAME", "euroleague.onrender.com")
    from euroleague_open_data.server_http import _public_hosts

    assert _public_hosts() == ["custom.example.org", "euroleague.onrender.com"]


def test_escape_hatch_disables_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """A locked-out deployment must be recoverable with one variable, not a rebuild."""
    monkeypatch.setenv("DISABLE_HOST_CHECK", "1")
    settings = _security_settings([SPACE])
    assert not settings.enable_dns_rebinding_protection
