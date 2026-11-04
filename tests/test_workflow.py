import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_manual_workflow_security_and_lifecycle():
    workflow = yaml.safe_load((ROOT / ".github/workflows/vpn.yml").read_text())
    # PyYAML implements YAML 1.1, where the unquoted key 'on' is a boolean.
    dispatch = workflow.get("on", workflow.get(True))["workflow_dispatch"]
    assert dispatch is None or dispatch == {}
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
    env = secret_steps[0]["env"]
    assert {"TG_BASE_URL", "TG_BOT_TOKEN", "TG_CHANNEL_USERNAME"} <= set(env)
    assert "VPN_USERS" not in env
    assert "VPN_TRANSPORT" not in env
    assert "python -m vpn_server" in secret_steps[0]["run"]
    assert "pull_request" not in str(workflow)
    assert any(
        "upload-artifact" in step.get("uses", "") and step["if"] == "${{ always() }}"
        for step in steps
    )


def test_independent_cleanup_job_runs_always():
    workflow = yaml.safe_load((ROOT / ".github/workflows/vpn.yml").read_text())
    cleanup = workflow["jobs"]["cleanup"]
    assert cleanup["needs"] == "vpn"
    assert cleanup["if"] == "${{ always() }}"
    assert cleanup["timeout-minutes"] == 5
    assert any("download-artifact" in step.get("uses", "") for step in cleanup["steps"])
    run = [step for step in cleanup["steps"] if "run" in step][-1]
    assert "python -m vpn_server.cleanup" in run["run"]
    assert set(run["env"]) == {"TG_BASE_URL", "TG_BOT_TOKEN", "TG_CHANNEL_USERNAME"}


def test_runtime_lockfile_is_hash_pinned_without_qr_dependency():
    lock = (ROOT / "requirements.txt").read_text()
    packages = re.findall(r"^([A-Za-z0-9_.-]+(?:\[[^\]]+\])?)==([^\s\\]+)", lock, re.MULTILINE)
    assert dict(packages)["python-telegram-bot"] == "22.8"
    assert "qrcode" not in lock
    assert "python-telegram-bot" in (ROOT / "requirements.in").read_text()
    assert lock.count("--hash=sha256:") >= len(packages)
