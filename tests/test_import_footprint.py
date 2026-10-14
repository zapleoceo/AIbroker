"""Agent workers must not pay for litellm they never call."""

import os
import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")


def test_run_worker_import_does_not_load_litellm():
    code = (
        "import sys, aibroker.agent.run_worker; "
        "sys.exit(1 if 'litellm' in sys.modules else 0)"
    )
    env = {**os.environ, "PYTHONPATH": SRC}
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
