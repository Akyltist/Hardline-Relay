import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.parametrize("agent, expected", [
    ("pilot:local", 0), ("pilot:remote", 0), ("pilot:berlin", 0),
    ("pilot:new_member", 0), ("pilot:ab", 0), ("pilot:" + "a" * 48, 0),
    ("pilot:Member", 1), ("pilot:new-member", 1), ("pilot:admin", 1),
    ("codex:new_member", 1), ("pilot:new_member\n", 1), ("pilot:a", 1),
    ("pilot:" + "a" * 49, 1), (["pilot:remote"], 1),
])
def test_client_accepts_only_pilot_identities(tmp_path, agent, expected):
    shell = shutil.which("powershell")
    if not shell:
        pytest.skip("Windows PowerShell is unavailable")
    repo = tmp_path / "repo"
    credentials = tmp_path / "credentials"
    (repo / "experiments").mkdir(parents=True)
    credentials.mkdir()
    (repo / "experiments" / "lan_tls_client.py").write_text("print('client started')\n")
    (credentials / "client.json").write_text(json.dumps({"agent": agent, "server_ip": "127.0.0.1"}))
    (credentials / "agent.token").write_text("sensitive-test-token")
    script = Path(__file__).resolve().parents[1] / "experiments" / "windows_lan_pilot_client.ps1"
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
                             "-RepoRoot", str(repo), "-CredentialDirectory", str(credentials),
                             "-PythonPath", sys.executable, "-ServerPort", "1234"],
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == expected
    assert ("client started" in result.stdout) is (expected == 0)
    assert "sensitive-test-token" not in result.stdout + result.stderr


@pytest.mark.parametrize("agent,enabled,expected_lane", [
    ("pilot:remote", True, "pilot:remote"),
    ("pilot:berlin", True, "pilot:berlin"),
    ("pilot:new_member", True, "pilot:new_member"),
    ("pilot:local", False, "<unset>"),
    ("pilot:remote", False, "<unset>"),
    ("pilot:new_member", False, "<unset>"),
])
def test_wake_lane_comes_from_client_configuration(tmp_path, agent, enabled, expected_lane):
    shell = shutil.which("powershell")
    if not shell:
        pytest.skip("Windows PowerShell is unavailable")
    repo = tmp_path / "repo"
    credentials = tmp_path / "credentials"
    (repo / "experiments").mkdir(parents=True)
    credentials.mkdir()
    (repo / "experiments" / "lan_tls_client.py").write_text(
        "import os\nprint(os.environ.get('HARDLINE_PILOT_DSH_WAKE_LANE', '<unset>'))\n")
    (credentials / "client.json").write_text(json.dumps({"agent": agent, "server_ip": "127.0.0.1"}))
    (credentials / "agent.token").write_text("sensitive-test-token")
    env = os.environ.copy()
    env.pop("HARDLINE_PILOT_DSH_WAKE_URL", None)
    env.pop("HARDLINE_PILOT_DSH_WAKE_TOKEN", None)
    env["HARDLINE_PILOT_DSH_WAKE_LANE"] = "pilot:berlin"  # untrusted inherited lane
    if enabled:
        env["HARDLINE_PILOT_DSH_WAKE_URL"] = "http://127.0.0.1:1234/notice"
        env["HARDLINE_PILOT_DSH_WAKE_TOKEN"] = "a" * 64
    script = Path(__file__).resolve().parents[1] / "experiments" / "windows_lan_pilot_client.ps1"
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
                             "-RepoRoot", str(repo), "-CredentialDirectory", str(credentials),
                             "-PythonPath", sys.executable, "-ServerPort", "1234"],
                            text=True, capture_output=True, timeout=30, env=env)
    assert result.returncode == 0
    assert result.stdout.strip() == expected_lane
    assert "sensitive-test-token" not in result.stdout + result.stderr
