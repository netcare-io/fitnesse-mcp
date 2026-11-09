import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_docs_tools_md_matches_the_tools():
    run = subprocess.run([sys.executable, "scripts/tools_list.py", "--check"], cwd=ROOT,
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stderr + "\nRegenerate it: python scripts/tools_list.py"
