import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]


def _snapshot(revision: str, state: str = "COMPLETED", *, pending: int = 0, desired: int = 2) -> dict[str, Any]:
    return {
        "failures": [],
        "services": [
            {
                "runningCount": desired,
                "desiredCount": desired,
                "pendingCount": pending,
                "deployments": [{"status": "PRIMARY", "taskDefinition": revision, "rolloutState": state}],
            }
        ],
    }


@pytest.mark.parametrize(
    ("snapshots", "success"),
    [
        ([_snapshot("expected")], True),
        ([_snapshot("old"), _snapshot("expected", "IN_PROGRESS"), _snapshot("expected")], True),
        ([_snapshot("expected", "IN_PROGRESS"), _snapshot("old")], False),
        ([_snapshot("expected", "FAILED")], False),
        ([_snapshot("expected", desired=0)], False),
        ([_snapshot("expected", desired=1)], False),
        ([_snapshot("expected", desired=3)], False),
        ([{"failures": [{"reason": "MISSING"}], "services": []}], False),
    ],
)
def test_rollout_requires_the_requested_revision(
    tmp_path: Path, snapshots: list[dict[str, Any]], success: bool
) -> None:
    (tmp_path / "snapshots.json").write_text(json.dumps(snapshots))
    aws = tmp_path / "aws"
    aws.write_text(
        """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root = Path(os.environ['MOCK_AWS_DIR'])
if sys.argv[2] == 'update-service':
    print('ow-celery-history')
else:
    count = root / 'count'
    index = int(count.read_text()) if count.exists() else 0
    snapshots = json.loads((root / 'snapshots.json').read_text())
    count.write_text(str(index + 1))
    print(json.dumps(snapshots[min(index, len(snapshots) - 1)]))
"""
    )
    aws.chmod(0o755)
    sleep = tmp_path / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n")
    sleep.chmod(0o755)
    result = subprocess.run(
        ["bash", str(REPO / "deploy/scripts/roll-ecs-service.sh"), "ow-celery-history", "expected", "2"],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "MOCK_AWS_DIR": str(tmp_path),
            "ECS_CLUSTER": "test",
        },
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert (result.returncode == 0) is success, result.stdout + result.stderr


def test_production_rolls_history_before_every_producer() -> None:
    workflow = yaml.safe_load((REPO / ".github/workflows/deploy-prod.yml").read_text())
    steps = workflow["jobs"]["deploy-app"]["steps"]
    rollout = next(step["run"] for step in steps if "Roll historical consumer" in step.get("name", ""))
    assert rollout.index("ow-celery-history:") < rollout.index("ow-celery-worker:") < rollout.index("ow-backend:")
    assert rollout.index("ow-celery-history:") < rollout.index("ow-celery-beat:")
    assert 'if [[ "$svc" == "ow-celery-history" ]]; then expected_count=2; fi' in rollout
    assert 'bash deploy/scripts/roll-ecs-service.sh "$svc" "$task_definition" "$expected_count"' in rollout


def test_compose_consumer_is_ready_before_rolling_producers() -> None:
    deploy = (REPO / "deploy/scripts/deploy.sh").read_text()
    assert deploy.index("--scale celery-history=1") < deploy.index('for svc in "${ROLLING_SERVICES[@]}"')
    assert 'wait_containers_healthy "$HEALTH_TIMEOUT_SECONDS" "$history_ids"' in deploy
    compose = yaml.safe_load((REPO / "deploy/docker-compose.yml").read_text())
    history = compose["services"]["celery-history"]
    assert history["command"] == "scripts/start/history-worker.sh"
    assert "app" not in history["depends_on"]
