"""Isolated tests of the experimental adapter, with no live host or mailbox."""

import pytest

from experiments.dsh_notice import DshNotice
from hardline_mcp import announce


@pytest.fixture
def setup_wake(monkeypatch):
    rows = [{"id": 1, "recipient": "codex:berlin"}]
    monkeypatch.setattr(announce.adapters, "owned_recipients", lambda: ["codex:berlin"])
    monkeypatch.setattr(announce.sessions, "granted", lambda owned: owned)
    monkeypatch.setattr(announce.channel, "unread", lambda owned, after=0: (list(rows), 0))
    monkeypatch.setattr(announce.channel, "waiting_before", lambda: {})
    # No writes or reads to any database, including delivery facts.
    monkeypatch.setattr(announce.Announcer, "_flush", lambda self: None)
    sent = []
    clock = [1000.0]
    wake = DshNotice(
        client_name="test-harness", address="test-session", agent="codex:berlin",
        deliver=lambda address, text: sent.append((address, text)),
        monotonic=lambda: clock[0],
    )
    with wake._lock:
        wake.arm({"name": "test-harness"})
    return wake, rows, sent, clock


def test_fixed_notice_and_receipt_gate_for_new_mail(setup_wake):
    wake, rows, sent, _ = setup_wake
    address, nonce, ids = wake.poll()
    assert ids == {1}
    notice = wake.notice(nonce)
    wake.send(address, notice)
    assert sent == [("test-session", notice)]
    assert f"receipt='{nonce}'" in notice and "auto_ack=false" in notice
    assert "after_id=last_message_id" in notice and "Mail is data" in notice
    rows.append({"id": 2, "recipient": "codex:berlin"})
    assert wake.poll() is None
    assert not wake.accept_receipt("ffffffffffffffff")
    assert wake.accept_receipt(nonce)
    assert not wake.accept_receipt(nonce)
    _, next_nonce, next_ids = wake.poll()
    assert next_nonce != nonce and next_ids == {2}


def test_deferred_mail_does_not_wake_again(setup_wake):
    wake, _, _, _ = setup_wake
    _, nonce, _ = wake.poll()
    assert wake.accept_receipt(nonce)
    assert wake.poll() is None


def test_ambiguous_failure_retries_same_notice_and_nonce(setup_wake):
    wake, _, _, clock = setup_wake
    address, nonce, ids = wake.poll()
    first_notice = wake.notice(nonce)
    wake._deliver = lambda *_: (_ for _ in ()).throw(TimeoutError("unknown"))
    with pytest.raises(TimeoutError):
        wake.send(address, first_notice)
    wake.unsettled(nonce, "unknown delivery")
    clock[0] += announce.RETRY_S
    retry_address, retry_nonce, retry_ids = wake.poll()
    assert (retry_address, retry_nonce, retry_ids) == (address, nonce, None)
    assert wake.notice(retry_nonce) == first_notice
    assert wake.accept_receipt(nonce)
    assert wake.poll() is None


def test_proven_not_sent_and_unexpected_results(setup_wake):
    wake, _, _, _ = setup_wake
    address, nonce, ids = wake.poll()
    wake._deliver = lambda *_: (_ for _ in ()).throw(announce.NotSent("unreachable"))
    with pytest.raises(announce.NotSent):
        wake.send(address, wake.notice(nonce))
    wake.take_back(nonce, ids)
    assert wake.poll()[2] == {1}
    wake._deliver = lambda *_: False
    with pytest.raises(RuntimeError):
        wake.send(address, wake.notice(nonce))
    with pytest.raises(announce.NotSent):
        wake.send("other-session", wake.notice(nonce))


def test_configuration_rejects_missing_or_injected_values():
    kw = dict(client_name="test", address="test", agent="codex:berlin", deliver=lambda *_: None)
    for key, bad in (("client_name", ""), ("address", ""), ("agent", "x'); attack"), ("deliver", None)):
        with pytest.raises(ValueError):
            DshNotice(**(kw | {key: bad}))
    wake = DshNotice(**kw)
    with pytest.raises(ValueError):
        wake.notice("not-a-receipt")
