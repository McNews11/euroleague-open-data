"""The landing page must quote the URL of the deployment actually serving it.

It is the page people copy the connection string from, so a stale hostname there is a
broken install for everyone who reads it -- and it fails quietly, because the page still
renders perfectly.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from euroleague_open_data.server_http import PLACEHOLDER_HOST, build_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_app(["testhost.example.com"]))


def test_placeholder_never_reaches_the_reader(client: TestClient) -> None:
    body = client.get("/", headers={"Host": "testhost.example.com"}).text
    assert PLACEHOLDER_HOST not in body


def test_serving_host_is_substituted(client: TestClient) -> None:
    """The scheme follows the request; the host is whatever answered it."""
    body = client.get("/", headers={"Host": "testhost.example.com"}).text
    assert "://testhost.example.com/mcp" in body


def test_forwarded_scheme_wins_over_the_connection(client: TestClient) -> None:
    """Behind a proxy the hop is plain HTTP, but the client's URL is https."""
    body = client.get(
        "/",
        headers={"Host": "testhost.example.com", "X-Forwarded-Proto": "https"},
    ).text
    assert "https://testhost.example.com/mcp" in body
    assert "http://testhost.example.com/mcp" not in body


def test_local_development_advertises_http(client: TestClient) -> None:
    body = client.get("/", headers={"Host": "127.0.0.1:7861"}).text
    assert "http://127.0.0.1:7861/mcp" in body
