"""Isolated runner checks: no real database, credentials or network listeners."""
import json
import os
from argparse import Namespace
from threading import Event

import pytest

from experiments import lan_pilot


GOOD_DSN = "host=127.0.0.1 port=55432 dbname=hardline_test user=hardline_test password=sample-secret"
SCHEMA = "test_lan_" + "a" * 32


def args(path, **overrides):
    values = dict(schema=SCHEMA, cert="cert.pem", key="key.pem", client_ca="ca.pem", bindings_file=str(path),
                  bind="127.0.0.1", port=0, allow_lan_test=False)
    values.update(overrides)
    return Namespace(**values)


@pytest.fixture
def bindings_file(tmp_path):
    path = tmp_path / "bindings.json"
    path.write_text(json.dumps({"a" * 64: "b" * 64}), encoding="utf-8")
    path.chmod(0o600)
    return path


class FakeGateway:
    def __init__(self):
        self.server_address = ("127.0.0.1", 19001)
        self.server_port = 19001
        self.stopped = Event()
        self.started = Event()
        self.closed = False

    def serve_forever(self):
        self.started.set()
        self.stopped.wait(3)

    def shutdown(self):
        self.stopped.set()

    def server_close(self):
        self.closed = True


class FakeTLS:
    server_port = 19002
    closed = False

    def serve_forever(self):
        pass

    def server_close(self):
        self.closed = True


@pytest.fixture
def stubs(monkeypatch):
    gateway, tls, state = FakeGateway(), FakeTLS(), {}
    monkeypatch.setenv("HARDLINE_TEST_POSTGRES_DSN", GOOD_DSN)
    monkeypatch.setattr(lan_pilot, "verify_existing_schema", lambda dsn, schema: state.update(schema=schema))
    monkeypatch.setattr(lan_pilot, "Mailbox", lambda dsn, schema: object())
    monkeypatch.setattr(lan_pilot, "create_gateway", lambda box: gateway)

    def tls_factory(**kwargs):
        state.update(tls=kwargs)
        return tls

    monkeypatch.setattr(lan_pilot, "create_tls_server", tls_factory)
    return gateway, tls, state


def test_runner_starts_and_stops_both_stub_servers(bindings_file, stubs, capsys):
    gateway, tls, state = stubs
    lan_pilot.run(args(bindings_file))
    assert gateway.started.is_set() and gateway.stopped.is_set() and gateway.closed and tls.closed
    assert state["schema"] == SCHEMA
    assert state["tls"]["backend_port"] == 19001
    assert state["tls"]["bindings"] == {"a" * 64: "b" * 64}
    output = capsys.readouterr().out
    assert "sample-secret" not in output and "a" * 64 not in output and "b" * 64 not in output
    assert "Windows ACLs are not checked" in output


@pytest.mark.parametrize("bad", ["host=127.0.0.1 port=55432 dbname=postgres user=hardline_test", GOOD_DSN.replace("55432", "5432"),
    GOOD_DSN.replace("127.0.0.1", "192.168.1.2"), GOOD_DSN + " hostaddr=8.8.8.8", GOOD_DSN + " options='-c search_path=public'", GOOD_DSN.replace("user=hardline_test", "user=postgres")])
def test_refuse_nonisolated_database(bad):
    with pytest.raises(ValueError):
        lan_pilot.isolated_dsn(bad)


def test_reject_bad_schema_and_lan_without_flag(bindings_file, stubs):
    with pytest.raises(ValueError):
        lan_pilot.run(args(bindings_file, schema="public"))
    with pytest.raises(ValueError):
        lan_pilot.run(args(bindings_file, bind="192.168.1.22"))
    assert not stubs[0].started.is_set()


def test_explicit_lan_consent_is_passed_to_stub_only(bindings_file, stubs):
    lan_pilot.run(args(bindings_file, bind="192.168.1.22", allow_lan_test=True))
    assert stubs[2]["tls"]["bind"] == "192.168.1.22"


@pytest.mark.parametrize("body", ["{}", "[]", '{"x":"y"}', '{"%s":"%s","%s":"%s"}' % ("a"*64, "b"*64, "a"*64, "c"*64)])
def test_invalid_or_duplicate_bindings_rejected(bindings_file, body):
    bindings_file.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError):
        lan_pilot.read_bindings(bindings_file)


@pytest.mark.skipif(os.name == "nt", reason="Windows ACL must be checked by the operator")
def test_posix_unsafe_permissions_rejected(bindings_file):
    bindings_file.chmod(0o644)
    with pytest.raises(ValueError):
        lan_pilot.read_bindings(bindings_file)


def test_proxy_creation_failure_closes_gateway_without_start(bindings_file, stubs, monkeypatch):
    monkeypatch.setattr(lan_pilot, "create_tls_server", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("sensitive")))
    with pytest.raises(RuntimeError):
        lan_pilot.run(args(bindings_file))
    assert stubs[0].closed and not stubs[0].started.is_set()


def test_main_hides_exception_content(bindings_file, stubs, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise RuntimeError("sensitive-credential")
    monkeypatch.setattr(lan_pilot, "verify_existing_schema", fail)
    result = lan_pilot.main(["--schema", SCHEMA, "--cert", "x", "--key", "x", "--client-ca", "x", "--bindings-file", str(bindings_file)])
    output = capsys.readouterr()
    assert result == 1 and "sensitive-credential" not in output.err + output.out


def test_schema_inspection_uses_only_selects(monkeypatch):
    class Cursor:
        def __init__(self, rows): self.rows = rows
        def fetchone(self): return self.rows[0]
        def fetchall(self): return self.rows

    class Conn:
        calls = []
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, query, params=None):
            self.calls.append(query)
            if len(self.calls) == 1:
                return Cursor([("hardline_test", "hardline_test")])
            return Cursor([(name,) for name in lan_pilot.REQUIRED_TABLES])

    conn = Conn()
    monkeypatch.setattr(lan_pilot.psycopg, "connect", lambda *a, **kw: conn)
    lan_pilot.verify_existing_schema("dummy", SCHEMA)
    assert len(conn.calls) == 2 and all(query.strip().upper().startswith("SELECT") for query in conn.calls)
