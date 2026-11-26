"""Cross-process prototype: fake DSH host only; never opens a live mailbox."""

import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

from hardline_mcp import dsh_wake


NODE = os.environ.get("DSH_TEST_NODE") or shutil.which("node")


def test_announcer_to_fake_dsh_host_preserves_receipt_and_coalesces_retry(monkeypatch):
    if not NODE or not Path(NODE).is_file():
        pytest.skip("local DSH Node runtime is unavailable")
    env = os.environ.copy()
    env["TEST_NOTICE_SECRET"] = secrets.token_hex(32)
    env.pop("PGPASSWORD", None)
    env.pop("HARDLINE_TEST_POSTGRES_DSN", None)
    proc = subprocess.Popen([str(NODE), "tests/fixtures/dsh_fake_host.mjs"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            ready = json.loads(pool.submit(proc.stdout.readline).result(timeout=10))
            assert ready["event"] == "ready"
            address = f"http://127.0.0.1:{ready['port']}/notice"
            monkeypatch.setenv("HARDLINE_DSH_WAKE_URL", address)
            monkeypatch.setenv("HARDLINE_DSH_WAKE_TOKEN", env["TEST_NOTICE_SECRET"])
            monkeypatch.setenv("HARDLINE_DSH_CLIENT_NAME", "dsh-mcp-client")
            monkeypatch.setenv("HARDLINE_DSH_WAKE_ROUTE", "test-route")
            monkeypatch.setenv("HARDLINE_AGENT", "codex")
            wake = dsh_wake.from_env()
            text = wake.notice("0123456789abcdef")
            wake.send(address, text)
            delivery = json.loads(pool.submit(proc.stdout.readline).result(timeout=10))
            assert delivery["event"] == "followup"
            assert delivery["message"]["source"]["kind"] == "hardline"
            assert "receipt='0123456789abcdef'" in delivery["message"]["content"][0]["text"]
            # The second identical request is accepted without another followup.
            wake.send(address, text)
            proc.stdin.close()
            closed = json.loads(pool.submit(proc.stdout.readline).result(timeout=10))
            assert closed == {"event": "closed", "delivered": 1}
    finally:
        if not proc.stdin.closed:
            proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        proc.stdout.close()
        proc.stderr.close()
