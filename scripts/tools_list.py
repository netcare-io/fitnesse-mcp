"""Writes docs/tools.md: every tool with its mode, area, extra gate and description.

Each toolset combination runs server.py in its own process, as the server reads its
environment at import. --check compares instead of writing (exit 1 if docs/tools.md is stale).

    python scripts/tools_list.py [--check]
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "tools.md"

_LIST = r'''
import asyncio, json, server
from fastmcp import Client
async def main():
    async with Client(server.mcp) as c:
        print(json.dumps({t.name: t.description for t in await c.list_tools()}))
asyncio.run(main())
'''

_FLAGS = {"FITNESSE_ALLOW_SHUTDOWN": "1", "FITNESSE_ALLOW_ACCOUNT_ADMIN": "1"}
_FILES = {"FITNESSE_FILES_ROOT": "files"}


def _tools(toolsets: str, **env: str) -> dict[str, str]:
    environ = {k: v for k, v in os.environ.items() if not k.startswith("FITNESSE_")}
    environ.update(FITNESSE_BASE_URL="http://fitnesse.invalid", FITNESSE_TOOLSETS=toolsets, **env)
    out = subprocess.run([sys.executable, "-c", _LIST], env=environ, cwd=ROOT,
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def render() -> str:
    everything = {**_FLAGS, **_FILES}
    full = _tools("all,debug,complete", **everything)
    modes = {m: set(_tools(f"{m},debug,complete", **everything)) for m in ("read", "write", "test")}
    without_complete = set(_tools("all,debug", **everything))
    without_debug = set(_tools("all,complete", **everything))
    without_files = set(_tools("all,debug,complete", **_FLAGS))
    without_flags = set(_tools("all,debug,complete", **_FILES))

    rows = []
    for name, description in full.items():
        mode = "/".join(m for m in ("read", "write", "test") if name in modes[m])
        area = "debug" if name not in without_debug else "complete" if name not in without_complete else "standard"
        extra = []
        if name not in without_files:
            extra.append("FILES_ROOT")
        if name not in without_flags:
            extra.append("ALLOW_SHUTDOWN" if "shutdown" in name else "ALLOW_ACCOUNT_ADMIN")
        text = " ".join((description or "").split()).replace("|", "\\|")
        rows.append((area, mode, name, ",".join(extra) or "–", text))
    rows.sort(key=lambda r: (("standard", "complete", "debug").index(r[0]), r[1], r[2]))

    lines = [
        f"# FitNesse MCP – tools, gates and descriptions ({len(rows)})",
        "",
        "Gates: mode (read/write/test) must be in FITNESSE_TOOLSETS; area complete/debug must be "
        "named as well; extra env gates as listed.",
        "",
    ]
    for area in ("standard", "complete", "debug"):
        lines += ["", f"## {area}", "", "| Tool | Mode | Extra gate | Description |", "|---|---|---|---|"]
        lines += [f"| `{n}` | {m} | {e} | {d} |" for a, m, n, e, d in rows if a == area]
    return "\n".join(lines) + "\n"


def main() -> int:
    text = render()
    if "--check" in sys.argv[1:]:
        if DOC.read_text(encoding="utf-8") != text:
            print(f"{DOC.relative_to(ROOT)} is out of date: run python scripts/tools_list.py", file=sys.stderr)
            return 1
        return 0
    DOC.parent.mkdir(exist_ok=True)
    DOC.write_text(text, encoding="utf-8")
    print(f"wrote {DOC.relative_to(ROOT)} ({len(text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
