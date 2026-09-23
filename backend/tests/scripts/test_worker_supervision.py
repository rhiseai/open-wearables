import os
import subprocess
from pathlib import Path


def test_worker_script_exits_when_a_required_worker_dies(tmp_path: Path) -> None:
    """A dead background worker must take down the container for orchestration."""
    mock_uv = tmp_path / "uv"
    cpu_started = tmp_path / "cpu-started"
    cpu_stopped = tmp_path / "cpu-stopped"
    mock_uv.write_text(
        """#!/bin/bash
set -eu
if [[ "$*" == *"io@%h"* ]]; then
  sleep 0.2
  exit 42
fi
touch "$CPU_STARTED"
trap 'touch "$CPU_STOPPED"; exit 0' TERM INT
while true; do sleep 0.1; done
"""
    )
    mock_uv.chmod(0o755)

    backend_dir = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{tmp_path}:{environment['PATH']}",
            "CPU_STARTED": str(cpu_started),
            "CPU_STOPPED": str(cpu_stopped),
        }
    )

    result = subprocess.run(
        ["bash", "scripts/start/worker.sh"],
        cwd=backend_dir,
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 42, result
    assert cpu_started.exists()
    assert cpu_stopped.exists()
    assert "A required Celery worker exited with status 42" in result.stderr
