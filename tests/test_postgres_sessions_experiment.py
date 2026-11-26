"""Disposable test database only; every test uses its own schema."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
psycopg = pytest.importorskip("psycopg")
from psycopg import sql

from experiments.postgres_sessions import Sessions


@pytest.fixture
def sessions():
    dsn = os.environ.get("HARDLINE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("isolated PostgreSQL DSN not configured")
    schema = "test_lan_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        if conn.execute("SELECT current_database()").fetchone()[0] != "hardline_test":
            pytest.fail("refusing non-test database")
        conn.execute(sql.SQL("CREATE SCHEMA {} ").format(sql.Identifier(schema)))
    try:
        store = Sessions(dsn, schema)
        store.initialize()
        yield store
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def expire(store, lane):
    with store.connect() as conn:
        conn.execute("UPDATE session_leases SET lease_until = clock_timestamp() - interval '1 second' WHERE lane = %s", (lane,))


def test_concurrent_claim_single_winner(sessions):
    barrier = Barrier(8)
    owners = [(uuid4(), uuid4()) for _ in range(8)]
    def attempt(owner):
        barrier.wait()
        return sessions.acquire("lane", *owner, 60)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, owners))
    assert results.count(1) == 1
    assert results.count(None) == 7


def test_reconnect_heartbeat_expiry_and_stale_fencing(sessions):
    host, session = uuid4(), uuid4()
    other_host, other_session = uuid4(), uuid4()
    token = sessions.acquire("do", host, session, 60)
    assert token == 1
    assert sessions.acquire("do", host, session, 60) == token
    assert sessions.acquire("do", other_host, other_session, 60) is None
    assert sessions.heartbeat("do", host, session, token, 60)
    assert not sessions.heartbeat("do", other_host, session, token, 60)
    assert sessions.fenced_increment("do", host, session, token) == 1
    expire(sessions, "do")
    assert not sessions.heartbeat("do", host, session, token, 60)
    assert sessions.fenced_increment("do", host, session, token) is None
    new_token = sessions.acquire("do", other_host, other_session, 60)
    assert new_token == token + 1
    assert sessions.fenced_increment("do", host, session, token) is None
    assert not sessions.heartbeat("do", host, session, token, 60)
    assert sessions.fenced_increment("do", other_host, other_session, new_token) == 2


def test_expired_owner_reacquires_with_new_generation(sessions):
    host, session = uuid4(), uuid4()
    old = sessions.acquire("do", host, session, 60)
    expire(sessions, "do")
    new = sessions.acquire("do", host, session, 60)
    assert new == old + 1
    assert sessions.fenced_increment("do", host, session, old) is None
    assert sessions.fenced_increment("do", host, session, new) == 1


def test_session_can_hold_multiple_lanes_with_independent_fencing(sessions):
    host, session = uuid4(), uuid4()
    assert sessions.acquire("do", host, session, 60) == 1
    assert sessions.acquire("other", host, session, 60) == 1
    expire(sessions, "do")
    assert sessions.acquire("do", uuid4(), uuid4(), 60) == 2
    assert sessions.fenced_increment("other", host, session, 1) == 1
    assert sessions.fenced_increment("do", host, session, 1) is None


def test_expiry_while_waiting_for_lock_rechecks_authority(sessions):
    host, session = uuid4(), uuid4()
    token = sessions.acquire("do", host, session, 60)
    with sessions.connect() as lock:
        lock.execute("SELECT lane FROM session_leases WHERE lane = 'do' FOR UPDATE")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(sessions.fenced_increment, "do", host, session, token)
            # Confirm that the worker actually reached the row lock before expiry.
            deadline = time.monotonic() + 5
            with sessions.connect() as observer:
                while time.monotonic() < deadline:
                    observer.execute("SELECT pg_stat_clear_snapshot()")
                    waiting = observer.execute("""SELECT EXISTS (
                        SELECT 1 FROM pg_stat_activity WHERE datname = current_database()
                        AND wait_event_type = 'Lock' AND query LIKE 'SELECT lane FROM session_leases WHERE lane = % FOR UPDATE'
                    )""").fetchone()[0]
                    if waiting:
                        break
                    time.sleep(0.02)
                if not waiting:
                    lock.rollback()
                    future.result(timeout=5)
                    pytest.fail("worker did not reach the lock")
            lock.execute("UPDATE session_leases SET lease_until = clock_timestamp() - interval '1 second' WHERE lane = 'do'")
            lock.commit()
            assert future.result(timeout=5) is None


def test_heartbeat_cannot_revive_lease_expired_while_waiting(sessions):
    host, session = uuid4(), uuid4()
    generation = sessions.acquire("do", host, session, 1)
    with sessions.connect() as lock:
        lock.execute("SELECT lane FROM session_leases WHERE lane = 'do' FOR UPDATE")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(sessions.heartbeat, "do", host, session, generation, 60)
            try:
                with pytest.raises(TimeoutError):
                    future.result(timeout=0.2)
                time.sleep(1.1)
            finally:
                lock.commit()
            assert future.result(timeout=5) is False


@pytest.mark.parametrize("ttl", [0, -1, 3601, True, 1.5])
def test_invalid_ttl_rejected(sessions, ttl):
    with pytest.raises(ValueError):
        sessions.acquire("do", uuid4(), uuid4(), ttl)
