import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_manual_workflow_security_and_lifecycle():
    workflow = yaml.safe_load((ROOT / ".github/workflows/vpn.yml").read_text())
    # PyYAML implements YAML 1.1, where the unquoted key 'on' is a boolean.
    assert workflow.get("on", workflow.get(True)) == {"workflow_dispatch": None}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    job = workflow["jobs"]["vpn"]
    assert job["timeout-minutes"] == 360
    assert job["runs-on"] == "ubuntu-24.04"
    steps = job["steps"]
    for step in steps:
        if "uses" in step:
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"])
    secret_steps = [
        step
        for step in steps
        if "env" in step and any(key.startswith("TG_") for key in step["env"])
    ]
    assert len(secret_steps) == 1
    assert set(secret_steps[0]["env"]) == {"TG_BASE_URL", "TG_BOT_TOKEN", "TG_CHANNEL_USERNAME"}
    assert all("secrets." in value for value in secret_steps[0]["env"].values())
    assert "python -m vpn_server" in secret_steps[0]["run"]
    assert "upload-artifact" not in str(workflow)
    assert "pull_request" not in str(workflow)
    assert steps[-1]["if"] == "${{ always() }}"


def test_runtime_lockfile_is_hash_pinned():
    lock = (ROOT / "requirements.txt").read_text()
    packages = re.findall(r"^([A-Za-z0-9_.-]+(?:\[[^\]]+\])?)==([^\s\\]+)", lock, re.MULTILINE)
    assert dict(packages)["python-telegram-bot"] == "22.8"
    assert "python-telegram-bot" in (ROOT / "requirements.in").read_text()
    assert lock.count("--hash=sha256:") >= len(packages)
