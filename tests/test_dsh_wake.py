"""Opt-in DSH integration with an isolated mailbox and a mocked host."""

import json

import anyio
import pytest

from hardline_mcp import announce, channel, dsh_wake, mailbox, server
from test_channel import LANE, serving, store  # noqa: F401


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("HARDLINE_DSH_WAKE_URL", "http://127.0.0.1:8791/notice")
    monkeypatch.setenv("HARDLINE_DSH_WAKE_TOKEN", "a" * 64)
    monkeypatch.setenv("HARDLINE_DSH_CLIENT_NAME", "dsh-mcp-client")
    monkeypatch.setenv("HARDLINE_DSH_WAKE_ROUTE", "test-route")
    monkeypatch.setenv("HARDLINE_AGENT", "claude")


def test_disabled_without_opt_in(monkeypatch):
    for key in dsh_wake._KEYS:
        monkeypatch.delenv(key, raising=False)
    assert dsh_wake.from_env() is None


@pytest.mark.parametrize("bad", ["http://localhost:8791/notice", "http://127.0.0.2:8791/notice",
    "https://127.0.0.1:8791/notice", "http://127.0.0.1/notice",
    "http://127.0.0.1:99999/notice", "http://user@127.0.0.1:8791/notice",
    "http://127.0.0.1:8791/notice?x=1", "http://127.0.0.1:8791/other"])
def test_bad_destinations_fail_closed(config, monkeypatch, bad):
    monkeypatch.setenv("HARDLINE_DSH_WAKE_URL", bad)
    with pytest.raises(ValueError, match="invalid DSH wake configuration"):
        dsh_wake.from_env()


@pytest.mark.parametrize("key,value", [("HARDLINE_DSH_WAKE_TOKEN", "secret"),
    ("HARDLINE_DSH_CLIENT_NAME", "claude-code"), ("HARDLINE_DSH_CLIENT_NAME", "codex-mcp-client"),
    ("HARDLINE_AGENT", ""), ("HARDLINE_AGENT", "unknown"),
    ("HARDLINE_DSH_WAKE_ROUTE", ""), ("HARDLINE_DSH_WAKE_ROUTE", "bad route")])
def test_incomplete_or_conflicting_config_is_rejected(config, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match="invalid DSH wake configuration"):
        dsh_wake.from_env()


@pytest.mark.parametrize("status,error", [(202, None), (403, announce.NotSent),
    (500, RuntimeError), (302, RuntimeError)])
def test_loopback_payload_contains_bound_route_and_agent(config, monkeypatch, status, error):
    requests = []
    class Response:
        pass
    class Connection:
        def __init__(self, host, port, timeout):
            assert (host, port) == ("127.0.0.1", 8791)
        def connect(self):
            pass
        def request(self, method, path, body, headers):
            requests.append((method, path, json.loads(body), headers))
        def getresponse(self):
            result = Response()
            result.status = status
            return result
        def close(self):
            pass
    monkeypatch.setattr(dsh_wake.http.client, "HTTPConnection", Connection)
    wake = dsh_wake.from_env()
    text = wake.notice("0123456789abcdef")
    if error:
        with pytest.raises(error):
            wake.send(wake.address, text)
    else:
        wake.send(wake.address, text)
    method, path, body, headers = requests[0]
    assert (method, path, body) == ("POST", "/notice", {"nonce": "0123456789abcdef", "agent": "claude", "route": "test-route"})
    assert headers["Authorization"] == "Bearer " + "a" * 64


@pytest.mark.anyio
async def test_serving_wakes_only_configured_client_and_receipts_rearm(store, config, monkeypatch):
    calls = []
    monkeypatch.setattr(dsh_wake.DshWake, "send", lambda self, address, text: calls.append(text))
    async with anyio.create_task_group() as tg:
        wire = await serving(tg, channel.Pusher(poll_s=0.02))
        await wire.handshake(client="dsh-mcp-client")
        mailbox.send("codex", LANE, "first")
        with anyio.fail_after(3):
            while len(calls) < 1:
                await anyio.sleep(0.01)
        nonce = calls[0].split("receipt='")[1].split("'")[0]
        assert "first" not in calls[0]
        mailbox.send("codex", LANE, "second")
        await anyio.sleep(0.15)
        assert len(calls) == 1
        assert (await server.inbox(agent="claude", auto_ack=False, receipt=nonce))["receipt"] == "accepted"
        with anyio.fail_after(3):
            while len(calls) < 2:
                await anyio.sleep(0.01)
        assert calls[0] != calls[1]
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_another_client_is_not_woken(store, config, monkeypatch):
    calls = []
    monkeypatch.setattr(dsh_wake.DshWake, "send", lambda self, address, text: calls.append(text))
    async with anyio.create_task_group() as tg:
        wire = await serving(tg, channel.Pusher(poll_s=0.02))
        await wire.handshake(client="other-client")
        mailbox.send("codex", LANE, "hello")
        await anyio.sleep(0.3)
        assert calls == []
        tg.cancel_scope.cancel()
