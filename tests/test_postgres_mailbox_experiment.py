"""Run only against a disposable PostgreSQL database explicitly supplied by the caller."""

import os
import json
import asyncio
import secrets
import subprocess
import sys
import time
from contextlib import AsyncExitStack
from http.client import HTTPConnection
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Thread
from uuid import uuid4

import pytest
psycopg = pytest.importorskip("psycopg")
from psycopg import sql

from experiments.postgres_mailbox import Mailbox


@pytest.fixture
def box():
    dsn = os.environ.get("HARDLINE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("isolated PostgreSQL DSN not configured")
    schema = "test_lan_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        database = conn.execute("SELECT current_database()").fetchone()[0]
        if database != "hardline_test":
            pytest.fail("refusing to test against a database other than hardline_test")
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    mailbox = Mailbox(dsn, schema)
    try:
        mailbox.initialize()
        from experiments.postgres_sessions import Sessions
        Sessions(dsn, schema).initialize()
        yield mailbox
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_independent_clients_and_ack_ownership(box):
    owner, other = uuid4(), uuid4()
    assert box.claim("codex:demo", owner)
    message = box.send("codex:demo", "Привет с другого клиента")
    assert box.inbox("codex:demo", other) == []
    assert not box.ack(message, "codex:demo", other)
    assert box.inbox("codex:demo", owner) == [(message, "Привет с другого клиента")]
    assert box.ack(message, "codex:demo", owner)
    assert not box.ack(message, "codex:demo", owner)
    assert box.inbox("codex:demo", owner) == []


def test_concurrent_claims_have_one_winner(box):
    owners = [uuid4() for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda owner: box.claim("codex:race", owner), owners))
    assert results.count(True) == 1


def test_concurrent_sends_are_all_durable(box):
    owner = uuid4()
    assert box.claim("codex:mail", owner)
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda n: box.send("codex:mail", str(n)), range(24)))
    assert len(set(ids)) == 24
    assert {row[0] for row in box.inbox("codex:mail", owner)} == set(ids)


def test_person_task_routes_only_to_assigned_avatar_even_when_offline(box):
    assert box.register_agent("codex:do", "avatar", "staff:123", "друг и наставник")
    assert box.register_agent("codex:fa", "agent")
    message_id = box.send_to_person("staff:123", "Задача сотруднику")
    do_owner, fa_owner = uuid4(), uuid4()
    assert box.claim("codex:do", do_owner)
    assert box.claim("codex:fa", fa_owner)
    assert box.inbox("codex:do", do_owner) == [(message_id, "Задача сотруднику")]
    assert box.inbox("codex:fa", fa_owner) == []


def test_person_identity_cannot_be_assigned_to_two_avatars(box):
    assert box.register_agent("codex:do", "avatar", "staff:123")
    assert not box.register_agent("codex:other", "avatar", "staff:123")
    with pytest.raises(LookupError):
        box.send_to_person("staff:unknown", "Неизвестному")


@pytest.mark.parametrize("kind,person", [("agent", "staff:123"), ("avatar", None),
                                        ("avatar", "  "), ("unknown", None)])
def test_registration_rejects_inconsistent_identity(box, kind, person):
    with pytest.raises(ValueError):
        box.register_agent("codex:bad", kind, person)


def test_authenticated_loopback_gateway_routes_and_isolates_mail(box):
    from experiments.loopback_gateway import create_server

    assert box.register_agent("codex:do", "avatar", "staff:123")
    assert box.register_agent("codex:remote", "agent")
    do_token = box.issue_credential("codex:do")
    remote_token = box.issue_credential("codex:remote")
    server = create_server(box)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    leases = {}

    def raw_request(token, endpoint, payload, lease=None):
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
            if lease:
                headers.update({"X-Test-Host": str(lease[0]), "X-Test-Session": str(lease[1]),
                                "X-Test-Generation": str(lease[2]), "X-Test-Session-Proof": lease[3]})
            conn.request("POST", endpoint, json.dumps(payload).encode("utf-8"),
                         headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def request(token, endpoint, payload):
        if token not in leases and token != "invalid":
            host, session, proof = uuid4(), uuid4(), secrets.token_urlsafe(32)
            status, result = raw_request(token, "/lease/acquire",
                                         {"host_id": str(host), "session_id": str(session), "session_proof": proof})
            assert status == 200
            leases[token] = (host, session, result["generation"], proof)
        return raw_request(token, endpoint, payload, leases.get(token))

    try:
        assert request("invalid", "/inbox", {})[0] == 401
        for endpoint, payload in (("/inbox", {}), ("/inbox-compatible", {"agent": "codex"}),
                                  ("/ack", {"message_id": 1}),
                                  ("/send", {"recipient": "codex:do", "body": "x"}),
                                  ("/send-person", {"recipient": "staff:123", "body": "x"}),
                                  ("/lease/heartbeat", {})):
            assert raw_request(remote_token, endpoint, payload)[0] == 403
        status, result = request(remote_token, "/send-person",
                                 {"recipient": "staff:123", "body": "Задача человеку"})
        assert status == 200
        message_id = result["message_id"]
        assert request(remote_token, "/inbox", {})[1]["messages"] == []
        assert request(remote_token, "/ack", {"message_id": message_id})[1] == {"acknowledged": False}
        assert request(do_token, "/inbox", {})[1]["messages"] == [[message_id, "Задача человеку", "codex:remote"]]
        assert request(do_token, "/ack", {"message_id": message_id})[1] == {"acknowledged": True}
        assert request(do_token, "/inbox", {})[1]["messages"] == []
        assert request(remote_token, "/send-person", {"recipient": "missing", "body": "x"})[0] == 404
        old_lease = leases[remote_token]
        forged_lease = (*old_lease[:3], secrets.token_urlsafe(32))
        assert raw_request(remote_token, "/inbox", {}, forged_lease)[0] == 403
        assert raw_request(remote_token, "/lease/acquire",
                           {"host_id": str(old_lease[0]), "session_id": str(old_lease[1]),
                            "session_proof": forged_lease[3]})[0] == 409
        with box.connect() as conn:
            conn.execute("UPDATE session_leases SET lease_until = clock_timestamp() - interval '1 second' WHERE lane = %s",
                         ("codex:remote",))
        next_host, next_session = uuid4(), uuid4()
        status, replacement = raw_request(remote_token, "/lease/acquire",
                                          {"host_id": str(next_host), "session_id": str(next_session),
                                           "session_proof": secrets.token_urlsafe(32)})
        assert status == 200 and replacement["generation"] > old_lease[2]
        for endpoint, payload in (("/inbox", {}), ("/inbox-compatible", {"agent": "codex"}),
                                  ("/ack", {"message_id": message_id}),
                                  ("/send", {"recipient": "codex:do", "body": "stale"}),
                                  ("/send-person", {"recipient": "staff:123", "body": "stale"}),
                                  ("/lease/heartbeat", {})):
            assert raw_request(remote_token, endpoint, payload, old_lease)[0] == 403
        assert request(do_token, "/inbox", {})[1]["messages"] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_two_separate_processes_exchange_person_message(box):
    from experiments.loopback_gateway import create_server

    assert box.register_agent("codex:do", "avatar", "staff:123")
    assert box.register_agent("codex:remote", "agent")
    do_token = box.issue_credential("codex:do")
    remote_token = box.issue_credential("codex:remote")
    from experiments.postgres_sessions import Sessions
    sessions = Sessions(box.dsn, box.schema)
    leases = {}
    for lane, token in (("codex:do", do_token), ("codex:remote", remote_token)):
        host, session, proof = uuid4(), uuid4(), secrets.token_urlsafe(32)
        leases[token] = (host, session, sessions.acquire(lane, host, session, 60, proof), proof)
    server = create_server(box)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    code = """import http.client, json, os, sys
request = json.load(sys.stdin)
conn = http.client.HTTPConnection('127.0.0.1', int(os.environ['TEST_GATEWAY_PORT']), timeout=5)
conn.request('POST', request['path'], json.dumps(request['payload']).encode('utf-8'),
             {'Authorization': 'Bearer ' + os.environ['TEST_GATEWAY_TOKEN'], 'Content-Type': 'application/json',
              'X-Test-Host': os.environ['TEST_HOST'], 'X-Test-Session': os.environ['TEST_SESSION'],
              'X-Test-Generation': os.environ['TEST_GENERATION'], 'X-Test-Session-Proof': os.environ['TEST_PROOF']})
response = conn.getresponse()
print(json.dumps({'status': response.status, 'body': json.loads(response.read())}))
conn.close()
"""

    def client(token, path, payload):
        env = os.environ.copy()
        env.pop("PGPASSWORD", None)
        env.pop("HARDLINE_TEST_POSTGRES_DSN", None)
        env["TEST_GATEWAY_PORT"] = str(server.server_port)
        env["TEST_GATEWAY_TOKEN"] = token
        env["TEST_HOST"], env["TEST_SESSION"], env["TEST_GENERATION"], env["TEST_PROOF"] = map(str, leases[token])
        result = subprocess.run([sys.executable, "-c", code], input=json.dumps({"path": path, "payload": payload}),
                                capture_output=True, text=True, encoding="utf-8", env=env, timeout=10, check=True)
        return json.loads(result.stdout)

    try:
        sent = client(remote_token, "/send-person", {"recipient": "staff:123", "body": "Привет",
                                                     "sender": "codex:do"})
        assert sent["status"] == 200
        received = client(do_token, "/inbox", {})
        assert received["body"]["messages"] == [[sent["body"]["message_id"], "Привет", "codex:remote"]]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_two_mcp_stdio_clients_exchange_and_isolate_person_mail(box):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from experiments.loopback_gateway import create_server

    assert box.register_agent("codex:do", "avatar", "staff:123")
    assert box.register_agent("codex:remote", "agent")
    do_token = box.issue_credential("codex:do")
    remote_token = box.issue_credential("codex:remote")
    server = create_server(box)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def exercise():
        async with AsyncExitStack() as stack:
            def contents(result):
                assert not result.isError
                if result.structuredContent is not None:
                    return result.structuredContent
                assert len(result.content) == 1 and result.content[0].type == "text"
                return json.loads(result.content[0].text)

            async def connect(token):
                env = os.environ.copy()
                env.pop("PGPASSWORD", None)
                env.pop("HARDLINE_TEST_POSTGRES_DSN", None)
                env["HARDLINE_LAN_TEST_PORT"] = str(server.server_port)
                env["HARDLINE_LAN_TEST_TOKEN"] = token
                params = StdioServerParameters(command=sys.executable,
                                               args=["-m", "experiments.mcp_loopback_client"], env=env)
                read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                return session

            do = await connect(do_token)
            remote = await connect(remote_token)
            tools = await remote.list_tools()
            assert {t.name for t in tools.tools} == {
                "experimental_send", "experimental_send_to_person", "experimental_inbox", "experimental_ack",
                "inbox", "ack"
            }
            sent = await remote.call_tool("experimental_send_to_person",
                                          {"person_id": "staff:123", "message": "Задача аватару"})
            message_id = contents(sent)["message_id"]
            other_inbox = await remote.call_tool("experimental_inbox", {})
            assert contents(other_inbox)["messages"] == []
            denied = await remote.call_tool("experimental_ack", {"message_id": message_id})
            assert contents(denied)["acknowledged"] is False
            inbox = await do.call_tool("inbox", {"agent": "codex", "auto_ack": False})
            message = contents(inbox)["messages"][0]
            assert (message["message_id"], message["body"], message["sender"]) == (
                message_id, "Задача аватару", "codex:remote"
            )
            ack = await do.call_tool("ack", {"message_id": message_id})
            assert contents(ack)["ok"] is True
            empty = await do.call_tool("experimental_inbox", {})
            assert contents(empty)["messages"] == []

    try:
        asyncio.run(exercise())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_compatible_inbox_pagination_receipts_and_explicit_ack(box):
    assert box.register_agent("codex:do", "avatar", "staff:123")
    token = box.issue_credential("codex:do")
    ids = [box.send("codex:do", str(n), sender="codex:fa") for n in range(3)]
    first = box.compatible_inbox(token, "codex", limit=2, auto_ack=False, receipt="unverified")
    assert [m["message_id"] for m in first["messages"]] == ids[:2]
    assert first["remaining"] == 3 and first["more"] is True and first["receipt"] == "unknown"
    assert all(m["acked_at"] is None for m in first["messages"])
    second = box.compatible_inbox(token, "codex:do", limit=2, auto_ack=False,
                                  after_id=first["last_message_id"])
    assert [m["message_id"] for m in second["messages"]] == ids[2:]
    assert second["remaining"] == 3 and second["more"] is False
    assert box.authenticated_ack(token, ids[0])
    browsing = box.compatible_inbox(token, "codex", unread_only=False, auto_ack=True)
    assert len(browsing["messages"]) == 3 and browsing["remaining"] == 2
    with pytest.raises(PermissionError):
        box.compatible_inbox(token, "codex:someone-else")


def test_compatible_inbox_concurrent_auto_ack_has_no_duplicate_delivery(box):
    assert box.register_agent("codex:do", "agent")
    token = box.issue_credential("codex:do")
    ids = [box.send("codex:do", str(n)) for n in range(20)]
    def consume(_):
        return box.compatible_inbox(token, "codex:do", limit=5, auto_ack=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        batches = list(pool.map(consume, range(4)))
    seen = [m["message_id"] for batch in batches for m in batch["messages"]]
    assert len(seen) == len(set(seen)) == 20
    assert set(seen) == set(ids)
    assert all(m["acked_at"] for batch in batches for m in batch["messages"])
    assert box.compatible_inbox(token, "codex:do")["remaining"] == 0


def test_fenced_mail_read_and_ack_reject_superseded_session(box):
    from experiments.postgres_sessions import Sessions

    sessions = Sessions(box.dsn, box.schema)
    assert box.register_agent("codex:do", "avatar", "staff:123")
    token = box.issue_credential("codex:do")
    message_id = box.send("codex:do", "Непрочитано")
    old_host, old_session = uuid4(), uuid4()
    old_generation = sessions.acquire("codex:do", old_host, old_session, 60)
    old_lease = (old_host, old_session, old_generation)
    assert box.compatible_inbox(token, "codex:do", auto_ack=False, lease=old_lease)["count"] == 1
    with sessions.connect() as conn:
        conn.execute("UPDATE session_leases SET lease_until = clock_timestamp() - interval '1 second'")
    new_host, new_session = uuid4(), uuid4()
    new_generation = sessions.acquire("codex:do", new_host, new_session, 60)
    assert new_generation == old_generation + 1
    with pytest.raises(PermissionError):
        box.compatible_inbox(token, "codex:do", auto_ack=True, lease=old_lease)
    with pytest.raises(PermissionError):
        box.authenticated_ack(token, message_id, lease=old_lease)
    new_lease = (new_host, new_session, new_generation)
    assert box.compatible_inbox(token, "codex:do", auto_ack=False, lease=new_lease)["count"] == 1
    assert box.authenticated_ack(token, message_id, lease=new_lease)


def test_send_waits_for_prior_recipient_commit_so_cursor_cannot_skip_mail(box):
    lane = "codex:do"
    with box.connect() as first:
        first.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lane,))
        first_id = first.execute("INSERT INTO messages(recipient, body) VALUES (%s, %s) RETURNING id",
                                 (lane, "first")).fetchone()[0]
        with ThreadPoolExecutor(max_workers=1) as pool:
            second = pool.submit(box.send, lane, "second")
            try:
                with pytest.raises(TimeoutError):
                    second.result(timeout=0.3)
            finally:
                first.commit()
            assert second.result(timeout=5) > first_id


def test_fenced_ack_rechecks_lease_after_waiting_for_mail_lock(box):
    from experiments.postgres_sessions import Sessions

    assert box.register_agent("codex:do", "agent")
    token = box.issue_credential("codex:do")
    message_id = box.send("codex:do", "unread")
    host, session = uuid4(), uuid4()
    sessions = Sessions(box.dsn, box.schema)
    lease = (host, session, sessions.acquire("codex:do", host, session, 1))
    with box.connect() as lock:
        lock.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("codex:do",))
        with ThreadPoolExecutor(max_workers=1) as pool:
            blocked = pool.submit(box.authenticated_ack, token, message_id, lease)
            try:
                with pytest.raises(TimeoutError):
                    blocked.result(timeout=0.2)
                time.sleep(1.1)
            finally:
                lock.commit()
            with pytest.raises(PermissionError):
                blocked.result(timeout=5)
    assert box.authenticated_inbox(token)[0][0] == message_id
