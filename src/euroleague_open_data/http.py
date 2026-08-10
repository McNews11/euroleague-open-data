"""Throttled, permanently-cached HTTP client for the EuroLeague upstream.

The upstream sits behind Cloudflare and rate-limits at the edge (error 1015). Measured
during recon on 2026-08-10:

* 3 req/s   -> blocked after ~30 requests
* 0.67 req/s -> still blocked, after ~50 requests
* cooldown   -> ~300 s, rolling and self-healing rather than escalating
* the limit is shared across api-live.euroleague.net and live.euroleague.net

So the client is deliberately slow and deliberately adaptive: it does not trust any
hard-coded rate, it converges on whatever the upstream currently tolerates.

See docs/api-notes.md section 6.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Politeness envelope. START is where we begin; the client never goes faster than FLOOR
# even after a long clean run, and never slower than CEILING before giving up.
START_INTERVAL = 6.0
FLOOR_INTERVAL = 4.0
CEILING_INTERVAL = 90.0

# After this many consecutive successes, try going slightly faster.
SPEEDUP_AFTER = 40
SPEEDUP_FACTOR = 0.9
SLOWDOWN_FACTOR = 1.8

USER_AGENT = (
    "euroleague-open-data/0.1 (+https://github.com/euroleague-open-data; "
    "research/educational; contact via repo issues)"
)


class RateLimitGaveUp(RuntimeError):
    """Raised when a URL kept returning 429 past the retry budget."""


@dataclass
class FetchStats:
    requests: int = 0
    cache_hits: int = 0
    rate_limit_hits: int = 0
    sleep_seconds: float = 0.0
    errors: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "cache_hits": self.cache_hits,
            "rate_limit_hits": self.rate_limit_hits,
            "sleep_seconds": round(self.sleep_seconds, 1),
            "errors": dict(self.errors),
        }


class ThrottledClient:
    """Fetches JSON with an adaptive delay and a permanent on-disk cache.

    The cache is permanent by design, not as an optimisation. A finished game from 2019 is
    immutable; refetching it spends quota that belongs to everyone else using the upstream.
    A cache hit costs no request and, importantly, no delay.
    """

    def __init__(
        self,
        cache_dir: Path,
        *,
        interval: float = START_INTERVAL,
        max_retries: int = 5,
    ) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.interval = interval
        self.max_retries = max_retries
        self.stats = FetchStats()
        self._last_request_at = 0.0
        self._consecutive_ok = 0
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=30.0,
            follow_redirects=True,
        )

    # -- cache -------------------------------------------------------------------

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode()).hexdigest()[:20]
        return self.cache_dir / f"{digest}.json"

    def _read_cache(self, url: str) -> Any | None:
        path = self._cache_path(url)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError:
            log.warning("corrupt cache entry, refetching: %s", path)
            path.unlink(missing_ok=True)
            return None
        return payload["body"]

    def _write_cache(self, url: str, body: Any) -> None:
        path = self._cache_path(url)
        path.write_text(json.dumps({"url": url, "fetched_at": time.time(), "body": body}))

    # -- pacing ------------------------------------------------------------------

    def _wait_turn(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.interval - elapsed
        if remaining > 0:
            time.sleep(remaining)
            self.stats.sleep_seconds += remaining

    def _on_success(self) -> None:
        self._consecutive_ok += 1
        if self._consecutive_ok >= SPEEDUP_AFTER and self.interval > FLOOR_INTERVAL:
            self.interval = max(FLOOR_INTERVAL, self.interval * SPEEDUP_FACTOR)
            self._consecutive_ok = 0
            log.info("clean run, easing interval to %.1fs", self.interval)

    def _on_rate_limit(self, retry_after: float) -> None:
        self._consecutive_ok = 0
        self.stats.rate_limit_hits += 1
        self.interval = min(CEILING_INTERVAL, self.interval * SLOWDOWN_FACTOR)
        wait = retry_after + 5.0
        log.warning(
            "429 from upstream; sleeping %.0fs and backing off to %.1fs between requests",
            wait,
            self.interval,
        )
        time.sleep(wait)
        self.stats.sleep_seconds += wait

    # -- fetch -------------------------------------------------------------------

    def get_json(self, url: str, *, allow_cache: bool = True) -> Any:
        """Return parsed JSON for ``url``, from cache when possible.

        Raises RateLimitGaveUp if the upstream keeps refusing past the retry budget.
        Returns None for a 404, which upstream uses for "this does not exist" rather
        than as an error (see /v2/.../standings in docs/api-notes.md).
        """
        if allow_cache:
            cached = self._read_cache(url)
            if cached is not None:
                self.stats.cache_hits += 1
                return cached

        for attempt in range(self.max_retries):
            self._wait_turn()
            self._last_request_at = time.monotonic()
            self.stats.requests += 1

            try:
                response = self._client.get(url)
            except httpx.HTTPError as exc:
                kind = type(exc).__name__
                self.stats.errors[kind] = self.stats.errors.get(kind, 0) + 1
                log.warning("transport error %s on %s (attempt %d)", kind, url, attempt + 1)
                time.sleep(min(CEILING_INTERVAL, self.interval * (attempt + 1)))
                continue

            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", 60))
                self._on_rate_limit(retry_after)
                continue

            if response.status_code == 404:
                self._on_success()
                return None

            if response.status_code >= 500:
                self.stats.errors["5xx"] = self.stats.errors.get("5xx", 0) + 1
                time.sleep(min(CEILING_INTERVAL, self.interval * (attempt + 1)))
                continue

            response.raise_for_status()
            try:
                body = response.json()
            except json.JSONDecodeError:
                self.stats.errors["bad_json"] = self.stats.errors.get("bad_json", 0) + 1
                log.warning("non-JSON body from %s", url)
                return None

            self._on_success()
            self._write_cache(url, body)
            return body

        raise RateLimitGaveUp(f"gave up after {self.max_retries} attempts: {url}")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ThrottledClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
