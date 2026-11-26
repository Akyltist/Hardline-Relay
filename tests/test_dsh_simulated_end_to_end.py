"""Isolated SQLite -> Hardline wake -> fake DSH host -> receipt/ack test."""

import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess

import anyio
import pytest

from hardline_mcp import channel, mailbox, server
from test_channel import LANE, serving, store  # noqa: F401


NODE = os.environ.get("DSH_TEST_NODE") or shutil.which("node")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_real_hardline_notice_fake_dsh_receipt_and_next_message(store, monkeypatch):
    if not NODE or not Path(NODE).is_file():
        pytest.skip("local DSH Node runtime is unavailable")
    secret = secrets.token_hex(32)
    env = os.environ.copy()
    env.update(TEST_NOTICE_SECRET=secret, TEST_NOTICE_AGENT="claude")
    proc = subprocess.Popen([str(NODE), "tests/fixtures/dsh_fake_host.mjs"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        with anyio.fail_after(10):
            ready = json.loads(await anyio.to_thread.run_sync(proc.stdout.readline))
        monkeypatch.setenv("HARDLINE_DSH_WAKE_URL", f"http://127.0.0.1:{ready['port']}/notice")
        monkeypatch.setenv("HARDLINE_DSH_WAKE_TOKEN", secret)
        monkeypatch.setenv("HARDLINE_DSH_CLIENT_NAME", "dsh-mcp-client")
        monkeypatch.setenv("HARDLINE_AGENT", "claude")
        async with anyio.create_task_group() as tg:
            wire = await serving(tg, channel.Pusher(poll_s=0.02))
            await wire.handshake(client="dsh-mcp-client")
            first = mailbox.send("codex", LANE, "first test message", db_path=store)
            with anyio.fail_after(10):
                delivery = json.loads(await anyio.to_thread.run_sync(proc.stdout.readline))
            text = delivery["message"]["content"][0]["text"]
            assert "first test message" not in text
            nonce = re.search(r"receipt='([0-9a-f]{16})'", text).group(1)
            inbox = await server.inbox(agent="claude", auto_ack=False, receipt=nonce)
            assert inbox["receipt"] == "accepted"
            assert [msg["message_id"] for msg in inbox["messages"]] == [first["message_id"]]
            assert (await server.ack(message_id=first["message_id"]))["ok"]
            mailbox.send("codex", LANE, "second test message", db_path=store)
            with anyio.fail_after(10):
                second = json.loads(await anyio.to_thread.run_sync(proc.stdout.readline))
            second_text = second["message"]["content"][0]["text"]
            second_nonce = re.search(r"receipt='([0-9a-f]{16})'", second_text).group(1)
            assert second_nonce != nonce
            assert (await server.inbox(agent="claude", auto_ack=False, receipt=second_nonce))["receipt"] == "accepted"
            tg.cancel_scope.cancel()
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        proc.stdout.close()
        proc.stderr.close()
