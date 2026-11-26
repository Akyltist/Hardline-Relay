"""The actual pilot runner, test PostgreSQL and real TLS on loopback only."""

from argparse import Namespace
import hashlib
import json
import os
import ssl
from threading import Event, Thread

from experiments import lan_pilot
from experiments import lan_tls_client as tls
from experiments import mcp_loopback_client as client
from test_lan_tls_proxy import certs  # noqa: F401
from test_postgres_mailbox_experiment import box  # noqa: F401


def test_real_runner_starts_sends_and_stops(box, certs, tmp_path, monkeypatch):
    assert box.register_agent("pilot:remote", "agent")
    assert box.register_agent("pilot:local", "agent")
    remote_token = box.issue_credential("pilot:remote")
    fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert((certs / "client.crt").read_text())).hexdigest()
    bindings_path = tmp_path / "bindings.json"
    bindings_path.write_text(json.dumps({fingerprint: hashlib.sha256(remote_token.encode()).hexdigest()}))
    bindings_path.chmod(0o600)
    values = Namespace(schema=box.schema, bind="127.0.0.1", port=0, allow_lan_test=False,
                       cert=str(certs / "server.crt"), key=str(certs / "server.key"),
                       client_ca=str(certs / "ca.crt"), bindings_file=str(bindings_path))
    real_create = lan_pilot.create_tls_server
    ready, state = Event(), {}

    def create(**kwargs):
        server = real_create(**kwargs)
        state["server"] = server
        ready.set()
        return server

    monkeypatch.setattr(lan_pilot, "create_tls_server", create)
    errors = []

    def run():
        try:
            lan_pilot.run(values)
        except Exception as exc:
            errors.append(exc)
            ready.set()

    thread = Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(10), "runner did not create listener"
    assert not errors
    try:
        for name, value in {"HARDLINE_LAN_TEST_HOST": "127.0.0.1",
                            "HARDLINE_LAN_TEST_PORT": str(state["server"].server_port),
                            "HARDLINE_LAN_TEST_CA": str(certs / "ca.crt"),
                            "HARDLINE_LAN_TEST_CERT": str(certs / "client.crt"),
                            "HARDLINE_LAN_TEST_KEY": str(certs / "client.key"),
                            "HARDLINE_LAN_TEST_TOKEN": remote_token}.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setattr(client, "_request", tls.tls_request)
        result = client.experimental_send("pilot:local", "runner test")
        assert isinstance(result["message_id"], int)
        assert box.authenticated_inbox(box.issue_credential("pilot:local")) == [
            (result["message_id"], "runner test", "pilot:remote")]
    finally:
        state["server"].shutdown()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert not errors
