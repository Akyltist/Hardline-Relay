"""Isolated PostgreSQL -> HTTP loopback -> mTLS -> test client round trip."""

from threading import Thread
import hashlib
import ssl

from experiments import lan_tls_client as tls
from experiments import mcp_loopback_client as client
from experiments.lan_tls_proxy import create_server as create_tls_server
from experiments.loopback_gateway import create_server as create_gateway
from test_lan_tls_proxy import certs  # noqa: F401
from test_postgres_mailbox_experiment import box  # noqa: F401


def test_two_synthetic_agents_exchange_and_cannot_ack_other_mail(box, certs, monkeypatch):
    assert box.register_agent("pilot:local", "agent")
    assert box.register_agent("pilot:remote", "agent")
    local_token = box.issue_credential("pilot:local")
    remote_token = box.issue_credential("pilot:remote")
    gateway = create_gateway(box)
    def fingerprint(name):
        return hashlib.sha256(ssl.PEM_cert_to_DER_cert((certs / name).read_text())).hexdigest()
    bindings = {fingerprint("client.crt"): hashlib.sha256(remote_token.encode()).hexdigest(),
                fingerprint("client2.crt"): hashlib.sha256(local_token.encode()).hexdigest()}
    proxy = create_tls_server(certfile=certs / "server.crt", keyfile=certs / "server.key",
                              client_ca=certs / "ca.crt", backend_port=gateway.server_port, bindings=bindings)
    threads = [Thread(target=service.serve_forever, daemon=True) for service in (gateway, proxy)]
    for thread in threads:
        thread.start()
    try:
        for name, value in {
            "HARDLINE_LAN_TEST_HOST": "127.0.0.1", "HARDLINE_LAN_TEST_PORT": str(proxy.server_port),
            "HARDLINE_LAN_TEST_CA": str(certs / "ca.crt"), "HARDLINE_LAN_TEST_CERT": str(certs / "client.crt"),
            "HARDLINE_LAN_TEST_KEY": str(certs / "client.key"), "HARDLINE_LAN_TEST_TOKEN": remote_token,
        }.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setattr(client, "_request", tls.tls_request)
        sent = client.experimental_send("pilot:local", "synthetic test")
        message_id = sent["message_id"]
        assert not client.experimental_ack(message_id)["acknowledged"]
        monkeypatch.setenv("HARDLINE_LAN_TEST_TOKEN", local_token)
        monkeypatch.setenv("HARDLINE_LAN_TEST_CERT", str(certs / "client2.crt"))
        monkeypatch.setenv("HARDLINE_LAN_TEST_KEY", str(certs / "client2.key"))
        rows = client.experimental_inbox()["messages"]
        assert rows == [[message_id, "synthetic test", "pilot:remote"]]
        assert client.experimental_ack(message_id)["acknowledged"]
        assert client.experimental_inbox()["messages"] == []
    finally:
        for service in (proxy, gateway):
            service.shutdown()
            service.server_close()
        for thread in threads:
            thread.join(timeout=5)
