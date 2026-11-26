"""Provisioning tests use fake database calls; no real DB or listener is started."""
import hashlib
import json
import re
import ssl
import subprocess

import pytest

from experiments import lan_pilot_provision as provisioner


DSN = "host=127.0.0.1 port=55432 dbname=hardline_test user=hardline_test password=test-secret"


@pytest.fixture
def pilot(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    seen = []

    def fake_database(dsn, schema):
        seen.append((dsn, schema))
        return {"local": "local-token-" + "a" * 32, "remote": "remote-token-" + "b" * 32}

    monkeypatch.setattr(provisioner, "_initialize_database", fake_database)
    schema = provisioner.provision(tmp_path, "pilot", "192.168.4.22", DSN,
                                   acknowledge_private_parent=True)
    return tmp_path / "pilot", schema, seen


def test_certificates_identity_binding_and_distribution(pilot):
    root, schema, seen = pilot
    assert re.fullmatch(r"test_lan_[0-9a-f]{32}", schema)
    assert seen[0][1] == schema and "127.0.0.1" in seen[0][0]
    assert json.loads((root / "server/pilot.json").read_text())["schema"] == schema
    server_cert = ssl._ssl._test_decode_cert(str(root / "server/identity.crt"))
    assert ("IP Address", "192.168.4.22") in server_cert["subjectAltName"]
    for kind in ("server", "local", "remote"):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_cert_chain(str(root / kind / "identity.crt"), str(root / kind / "identity.key"))
        subprocess.run([provisioner._openssl(), "verify", "-purpose", "sslserver" if kind == "server" else "sslclient",
                        "-CAfile", str(root / "server/ca.crt"), str(root / kind / "identity.crt")],
                       check=True, capture_output=True)
    bindings = json.loads((root / "server/bindings.json").read_text())
    for kind in ("local", "remote"):
        cert_hash = provisioner._fingerprint(root / kind / "identity.crt")
        token = (root / kind / "agent.token").read_text().strip()
        assert bindings[cert_hash] == hashlib.sha256(token.encode("ascii")).hexdigest()
    assert len(bindings) == 2
    assert provisioner._fingerprint(root / "remote/identity.crt") != provisioner._fingerprint(root / "local/identity.crt")
    assert (root / "remote/agent.token").read_text() != (root / "local/agent.token").read_text()
    assert {item.name for item in (root / "remote").iterdir()} == {
        "identity.key", "identity.crt", "identity.csr", "certificate-ext.cnf", "ca.crt", "agent.token", "client.json"}
    remote = b"\n".join(p.read_bytes() for p in (root / "remote").iterdir())
    for secret in ((root / "server/ca.key").read_bytes(), (root / "server/identity.key").read_bytes(),
                   (root / "local/agent.token").read_bytes().strip(), b"test-secret"):
        assert secret not in remote


def test_mismatched_key_fails(pilot):
    root, _, _ = pilot
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    with pytest.raises(ssl.SSLError):
        context.load_cert_chain(str(root / "remote/identity.crt"), str(root / "local/identity.key"))


def test_existing_directory_never_overwritten(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    target = tmp_path / "occupied"
    target.mkdir()
    sentinel = target / "existing"
    sentinel.write_text("untouched")
    monkeypatch.setattr(provisioner, "_initialize_database", lambda *args: pytest.fail("unexpected database call"))
    with pytest.raises(FileExistsError):
        provisioner.provision(tmp_path, "occupied", "127.0.0.1", DSN, acknowledge_private_parent=True)
    assert sentinel.read_text() == "untouched"


@pytest.mark.parametrize("name", ["../other", ".", "..", "other/name", "other\\name", "C:thing"])
def test_reject_invalid_name_before_creation(tmp_path, name):
    tmp_path.chmod(0o700)
    with pytest.raises(ValueError):
        provisioner.provision(tmp_path, name, "127.0.0.1", DSN, acknowledge_private_parent=True)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("address,dsn,ack", [
    ("0.0.0.0", DSN, True),
    ("127.0.0.1", "host=example.com port=55432 dbname=hardline_test user=hardline_test", True),
    ("127.0.0.1", DSN, False),
])
def test_invalid_inputs_never_create_directories(tmp_path, address, dsn, ack):
    tmp_path.chmod(0o700)
    with pytest.raises(ValueError):
        provisioner.provision(tmp_path, "pilot", address, dsn, acknowledge_private_parent=ack)
    assert not list(tmp_path.iterdir())


def test_cli_suppresses_secrets_on_failure(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("HARDLINE_TEST_POSTGRES_DSN", DSN)
    monkeypatch.setattr(provisioner, "provision", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("test-secret")))
    assert provisioner.main(["--parent", str(tmp_path), "--name", "x", "--server-ip", "127.0.0.1", "--acknowledge-private-parent"]) == 1
    assert "test-secret" not in capsys.readouterr().err


def test_schema_created_once_and_only_two_pilot_agents(monkeypatch):
    commands, calls = [], []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query):
            commands.append(query)
            return self

        def fetchone(self):
            return ("hardline_test", "hardline_test")

    class Box:
        def __init__(self, dsn, schema):
            calls.append(("box", dsn, schema))

        def initialize(self):
            calls.append(("mailbox_initialized",))

        def register_agent(self, name, kind):
            calls.append(("register", name, kind))
            return True

        def issue_credential(self, name):
            return "token-for-" + name

    class Lease:
        def __init__(self, dsn, schema):
            calls.append(("session", dsn, schema))

        def initialize(self):
            calls.append(("sessions_initialized",))

    monkeypatch.setattr(provisioner.psycopg, "connect", lambda dsn, autocommit: Conn())
    monkeypatch.setattr(provisioner, "Mailbox", Box)
    monkeypatch.setattr(provisioner, "Sessions", Lease)
    schema = "test_lan_" + "a" * 32
    tokens = provisioner._initialize_database(DSN, schema)
    assert tokens == {"local": "token-for-pilot:local", "remote": "token-for-pilot:remote"}
    assert [x for x in calls if x[0] == "register"] == [("register", "pilot:local", "agent"), ("register", "pilot:remote", "agent")]
    assert "CREATE SCHEMA" in str(commands[1]) and "IF NOT EXISTS" not in str(commands[1])
