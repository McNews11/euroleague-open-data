"""Wait for the running crawl to exit, then crawl the previous season.

Two crawls at once would double the request rate and trip the Cloudflare limit, so this
chains rather than parallelises.
"""
import os, subprocess, sys, time
from pathlib import Path

pid = int(Path("data/crawl.pid").read_text().strip())
while True:
    try:
        os.kill(pid, 0)
        time.sleep(20)
    except ProcessLookupError:
        break
print(f"crawl {pid} finished; starting E2024", flush=True)
subprocess.run([".venv/bin/python", "-m", "euroleague_open_data.crawl",
                "--season", "E2024", "--report", "data/crawl-report-E2024.json"], check=False)
