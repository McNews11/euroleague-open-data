"""The serving path must not need the crawler's dependencies.

The deployed image installs `.` and not `.[etl]`, so httpx, polars and pyarrow are simply
absent from it. A module-scope import of any of them anywhere under server_http kills the
container at startup -- which is exactly what happened when the rosters module was added:
`ModuleNotFoundError: No module named 'httpx'`, before a single request was served.

Locally every dependency is installed, so nothing catches this by accident. The check
therefore runs in a subprocess with those modules blocked, reproducing the container.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

FORBIDDEN = ("httpx", "polars", "pyarrow")

PROGRAM = textwrap.dedent(
    f"""
    import sys

    class Blocked:
        \"\"\"Stand in for the packages the deployed image does not install.\"\"\"
        def find_module(self, name, path=None):
            return self.find_spec(name, path)

        def find_spec(self, name, path=None, target=None):
            root = name.split(".")[0]
            if root in {FORBIDDEN!r}:
                raise ImportError(
                    f"{{root}} is not installed in the serving image"
                )
            return None

    sys.meta_path.insert(0, Blocked())

    import euroleague_open_data.server_http as srv

    assert srv.build_app is not None
    for mod in {FORBIDDEN!r}:
        assert mod not in sys.modules, mod + " was imported by the serving path"
    print("ok")
    """
)


def test_server_imports_without_crawler_dependencies() -> None:
    result = subprocess.run(
        [sys.executable, "-c", PROGRAM],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "the serving path pulled in a crawler-only dependency:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "ok" in result.stdout


def test_rosters_sql_is_importable_without_httpx() -> None:
    """The MCP tool needs only this module's SQL, never its crawler."""
    program = textwrap.dedent(
        """
        import sys

        class Blocked:
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] == "httpx":
                    raise ImportError("httpx absent")
                return None

        sys.meta_path.insert(0, Blocked())
        from euroleague_open_data.rosters import TRANSFERS_SQL, UNSIGNED_SQL
        assert "announced_rosters" in TRANSFERS_SQL
        assert "announced_rosters" in UNSIGNED_SQL
        print("ok")
        """
    )
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
