"""Local-only tests: synthetic mailbox, mocked receiver, no live connections."""
import asyncio
import json
import re
from threading import Event
from urllib import error

import pytest

from experiments import lan_tls_client as tls
from experiments.lan_notice_loop import NoticeLoop


URL = "http://127.0.0.1:12345/notice"
TOKEN = "a" * 64


@pytest.fixture(autouse=True)
def clear_config(monkeypatch):
    monkeypatch.delenv("HARDLINE_PILOT_DSH_WAKE_URL", raising=False)
    monkeypatch.delenv("HARDLINE_PILOT_DSH_WAKE_TOKEN", raising=False)
    monkeypatch.delenv("HARDLINE_PILOT_DSH_WAKE_LANE", raising=False)


def configured(monkeypatch, url=URL, token=TOKEN, lane="pilot:remote"):
    monkeypatch.setenv("HARDLINE_PILOT_DSH_WAKE_URL", url)
    monkeypatch.setenv("HARDLINE_PILOT_DSH_WAKE_TOKEN", token)
    monkeypatch.setenv("HARDLINE_PILOT_DSH_WAKE_LANE", lane)


@pytest.mark.parametrize("lane", ["pilot:local", "pilot:remote", "pilot:berlin", "pilot:new_member"])
def test_supported_wake_lanes(monkeypatch, lane):
    configured(monkeypatch, lane=lane)
    assert tls._wake_configuration() == (URL, TOKEN, lane)


@pytest.mark.parametrize("lane", ["", "pilot:Member", "pilot:remote ", "other", "pilot:admin",
    "codex:new_member", "pilot:new_member\n", "pilot:" + "a" * 49])
def test_invalid_lane_rejected(monkeypatch, lane):
    configured(monkeypatch, lane=lane)
    with pytest.raises(RuntimeError, match="invalid pilot DSH wake configuration"):
        tls.create_test_mcp()


def test_missing_lane_rejected_when_wake_enabled(monkeypatch):
    configured(monkeypatch)
    monkeypatch.delenv("HARDLINE_PILOT_DSH_WAKE_LANE")
    with pytest.raises(RuntimeError, match="invalid pilot DSH wake configuration"):
        tls.create_test_mcp()


def test_disabled_and_partial_configuration(monkeypatch):
    assert tls._wake_configuration() is None
    assert tls.create_test_mcp().settings.lifespan is None
    monkeypatch.setenv("HARDLINE_PILOT_DSH_WAKE_URL", URL)
    with pytest.raises(RuntimeError, match="invalid pilot DSH wake configuration"):
        tls.create_test_mcp()
    monkeypatch.delenv("HARDLINE_PILOT_DSH_WAKE_URL")
    monkeypatch.setenv("HARDLINE_PILOT_DSH_WAKE_TOKEN", TOKEN)
    with pytest.raises(RuntimeError):
        tls.create_test_mcp()
    monkeypatch.delenv("HARDLINE_PILOT_DSH_WAKE_TOKEN")
    monkeypatch.setenv("HARDLINE_PILOT_DSH_WAKE_LANE", "pilot:new_member")
    with pytest.raises(RuntimeError):
        tls.create_test_mcp()


@pytest.mark.parametrize("url", ["", "https://127.0.0.1:123/notice",
    "http://localhost:123/notice", "http://127.0.0.2:123/notice",
    "http://user@127.0.0.1:123/notice", "http://127.0.0.1:123/notice?x=1",
    "http://127.0.0.1:123/notice#x", "http://127.0.0.1:0/notice",
    "http://127.0.0.1:65536/notice", "http://127.0.0.1/notice"])
def test_invalid_urls_rejected(monkeypatch, url):
    configured(monkeypatch, url=url)
    with pytest.raises(RuntimeError):
        tls.create_test_mcp()


@pytest.mark.parametrize("token", ["", "a" * 63, "z" * 64, "A" * 64, "a" * 65, "a" * 63 + "\n"])
def test_invalid_tokens_rejected(monkeypatch, token):
    configured(monkeypatch, token=token)
    with pytest.raises(RuntimeError):
        tls.create_test_mcp()


def test_post_sends_only_nonce_and_bearer(monkeypatch):
    captured = []

    class Response:
        status = 202
        def __enter__(self): return self
        def __exit__(self, *_args): pass

    class Opener:
        def open(self, req, timeout):
            captured.append((req, timeout))
            return Response()

    monkeypatch.setattr(tls.request, "build_opener", lambda *_args: Opener())
    assert tls._post_wake(URL, TOKEN)
    req, timeout = captured[0]
    assert req.full_url == URL and req.method == "POST" and timeout == 15
    assert req.get_header("Authorization") == "Bearer " + TOKEN
    payload = json.loads(req.data)
    assert set(payload) == {"nonce"} and re.fullmatch(r"[0-9a-f]{16}", payload["nonce"])


@pytest.mark.parametrize("status,known", [(401, True), (429, True), (500, False)])
def test_http_failures_distinguish_unknown_outcome(monkeypatch, status, known):
    class Opener:
        def open(self, req, timeout):
            raise error.HTTPError(req.full_url, status, "private", {}, None)
    monkeypatch.setattr(tls.request, "build_opener", lambda *_args: Opener())
    if known:
        assert tls._post_wake(URL, TOKEN) is False
    else:
        with pytest.raises(RuntimeError, match="unknown pilot wake outcome"):
            tls._post_wake(URL, TOKEN)


@pytest.mark.parametrize("lane", ["pilot:remote", "pilot:berlin", "pilot:new_member"])
def test_paginated_inbox_shares_gateway_without_ack(monkeypatch, lane):
    calls = []
    notices = []

    def gateway(path, payload):
        calls.append((path, payload))
        if not payload["after_id"]:
            return {"messages": [{"message_id": 1, "body": "secret"}], "more": True}
        return {"messages": [{"message_id": 2}], "more": False}

    async def notify(notice):
        notices.append(notice)
        return True

    monkeypatch.setattr(tls.client, "gateway_call", gateway)
    loop = NoticeLoop(lambda after_id: tls._read_pilot_unread(after_id, lane), notify)
    assert asyncio.run(loop.poll_once())
    assert [x[1]["after_id"] for x in calls] == [0, 1]
    assert all(path == "/inbox-compatible" and p["agent"] == lane
               and p["auto_ack"] is False and p["unread_only"] is True for path, p in calls)
    assert len(notices) == 1 and "secret" not in notices[0]


def test_wrong_credential_lane_cannot_wake_another_agent(monkeypatch):
    notices = []

    def gateway(path, payload):
        # Simulates the gateway's credential-to-lane enforcement.
        if payload["agent"] != "pilot:remote":
            raise RuntimeError("forbidden")
        return {"messages": [], "more": False}

    async def notify(notice):
        notices.append(notice)
        return True

    monkeypatch.setattr(tls.client, "gateway_call", gateway)
    loop = NoticeLoop(lambda after_id: tls._read_pilot_unread(after_id, "pilot:new_member"), notify)
    with pytest.raises(RuntimeError, match="forbidden"):
        asyncio.run(loop.poll_once())
    assert notices == []


def test_mcp_lifecycle_starts_and_stops_same_process_poller(monkeypatch):
    configured(monkeypatch)
    started = []
    stopped = []

    async def run(self, stop, *, interval=5):
        started.append(self)
        await stop.wait()
        stopped.append(self)

    monkeypatch.setattr(NoticeLoop, "run", run)
    mcp = tls.create_test_mcp()

    async def exercise():
        async with mcp.settings.lifespan(mcp):
            await asyncio.sleep(0)
            assert len(started) == 1 and not stopped
        assert stopped == started

    asyncio.run(exercise())


@pytest.mark.parametrize("lane", ["pilot:remote", "pilot:berlin", "pilot:new_member"])
def test_lifespan_drives_gateway_to_receiver_without_real_network(monkeypatch, lane):
    configured(monkeypatch, lane=lane)
    delivered = Event()
    reads = []
    sends = []

    def gateway(path, payload):
        reads.append((path, payload))
        return {"messages": [{"message_id": 7, "body": "private"}], "more": False}

    class Response:
        status = 202
        def __enter__(self): return self
        def __exit__(self, *_args): pass

    class Opener:
        def open(self, req, timeout):
            sends.append(json.loads(req.data))
            delivered.set()
            return Response()

    monkeypatch.setattr(tls.client, "gateway_call", gateway)
    monkeypatch.setattr(tls.request, "build_opener", lambda *_args: Opener())
    mcp = tls.create_test_mcp()

    async def exercise():
        async with mcp.settings.lifespan(mcp):
            assert await asyncio.to_thread(delivered.wait, 2)
        assert len(reads) == len(sends) == 1
        assert reads[0][1]["auto_ack"] is False
        assert reads[0][1]["agent"] == lane
        assert set(sends[0]) == {"nonce"}

    asyncio.run(exercise())
