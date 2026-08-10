"""Crawl a queue of seasons one after another.

Sequential rather than parallel on purpose: the upstream rate-limits both hosts together
at roughly 10 requests/minute, so two crawls at once would halve neither's runtime and
would trip Cloudflare for both.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

QUEUE = [("E", "E2024"), ("U", "U2025"), ("U", "U2024")]


def wait_for(pid: int) -> None:
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(20)


pid_file = Path("data/crawl.pid")
if pid_file.exists():
    running = int(pid_file.read_text().strip())
    print(f"waiting for crawl {running}", flush=True)
    wait_for(running)

for comp, season in QUEUE:
    print(f"=== starting {season} ===", flush=True)
    result = subprocess.run(
        [
            ".venv/bin/python", "-m", "euroleague_open_data.crawl",
            "--competition", comp,
            "--season", season,
            "--report", f"data/crawl-report-{season}.json",
        ],
        check=False,
    )
    print(f"=== {season} exited {result.returncode} ===", flush=True)

print("queue complete", flush=True)
sys.exit(0)
