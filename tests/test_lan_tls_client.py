"""The test client must fail closed and not leak test credentials on errors."""

import io
import hashlib
import ssl
from threading import Thread
from urllib import error

import pytest

from experiments import lan_tls_client as tls
from experiments.lan_tls_proxy import create_server
from test_lan_tls_proxy import backend, certs, proxy  # noqa: F401


@pytest.fixture
def config(monkeypatch):
    for name, value in {
        "HARDLINE_LAN_TEST_HOST": "192.168.1.10", "HARDLINE_LAN_TEST_PORT": "10001",
        "HARDLINE_LAN_TEST_TOKEN": "a" * 43, "HARDLINE_LAN_TEST_CA": "ca",
        "HARDLINE_LAN_TEST_CERT": "cert", "HARDLINE_LAN_TEST_KEY": "key",
    }.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize("host", ["", "example.com", "8.8.8.8", "0.0.0.0", "::1", "224.0.0.1", "169.254.1.1", "192.0.2.2"])
def test_invalid_host_rejected(config, monkeypatch, host):
    monkeypatch.setenv("HARDLINE_LAN_TEST_HOST", host)
    with pytest.raises(RuntimeError, match="invalid test gateway configuration"):
        tls._configuration()


@pytest.mark.parametrize("name,value", [("HARDLINE_LAN_TEST_PORT", "0"),
    ("HARDLINE_LAN_TEST_PORT", "65536"), ("HARDLINE_LAN_TEST_TOKEN", "short"),
    ("HARDLINE_LAN_TEST_CA", ""), ("HARDLINE_LAN_TEST_KEY", "")])
def test_invalid_config_rejected(config, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match="invalid test gateway configuration"):
        tls._configuration()


def test_request_builds_validated_tls_and_sends_proof(config, monkeypatch):
    class Context:
        def load_cert_chain(self, certfile, keyfile):
            assert (certfile, keyfile) == ("cert", "key")
    context = Context()
    monkeypatch.setattr(tls.ssl, "create_default_context", lambda purpose, cafile: context)
    seen = []
    class Response(io.BytesIO):
        pass
    class Opener:
        def open(self, req, timeout):
            seen.append((req, timeout))
            return Response(b'{"ok": true}')
    monkeypatch.setattr(tls.request, "build_opener", lambda *args: Opener())
    assert tls.tls_request("/inbox", {}, 2) == {"ok": True}
    req, timeout = seen[0]
    assert req.full_url == "https://192.168.1.10:10001/inbox"
    assert timeout == 5
    assert req.get_header("Authorization") == "Bearer " + "a" * 43
    assert req.get_header("X-test-generation") == "2"
    assert req.get_header("X-test-session-proof") == tls.client._SESSION_PROOF


def test_redirects_and_unauthenticated_server_do_not_expose_credentials(config, monkeypatch):
    class Context:
        def load_cert_chain(self, **kwargs):
            pass
    monkeypatch.setattr(tls.ssl, "create_default_context", lambda *args, **kwargs: Context())
    class Opener:
        def open(self, req, timeout):
            raise error.URLError("sensitive inner detail")
    monkeypatch.setattr(tls.request, "build_opener", lambda *args: Opener())
    with pytest.raises(RuntimeError) as caught:
        tls.tls_request("/inbox", {})
    assert "sensitive" not in str(caught.value)
    assert "a" * 43 not in str(caught.value)


def test_arbitrary_operation_refused(config):
    with pytest.raises(RuntimeError, match="invalid test gateway operation"):
        tls.tls_request("/admin", {})


def test_automatic_ack_is_refused_in_network_pilot(config):
    with pytest.raises(RuntimeError, match="auto_ack=false"):
        tls.tls_request("/inbox-compatible", {"agent": "example", "auto_ack": True})


def test_busy_lease_is_explained_without_forced_takeover(config, monkeypatch):
    class Context:
        def load_cert_chain(self, **kwargs):
            pass
    monkeypatch.setattr(tls.ssl, "create_default_context", lambda *args, **kwargs: Context())
    class Opener:
        def open(self, req, timeout):
            raise error.HTTPError(req.full_url, 409, "conflict", {}, None)
    monkeypatch.setattr(tls.request, "build_opener", lambda *args: Opener())
    with pytest.raises(RuntimeError, match="up to 60 seconds"):
        tls.tls_request("/lease/acquire", {})


def test_mcp_inbox_defaults_to_non_consuming_read():
    tool = tls.create_test_mcp()._tool_manager.get_tool("inbox")
    assert tool.parameters["properties"]["auto_ack"]["default"] is False


def test_actual_mutual_tls_and_server_certificate_verification(config, monkeypatch, certs, backend, proxy):
    monkeypatch.setenv("HARDLINE_LAN_TEST_HOST", "127.0.0.1")
    monkeypatch.setenv("HARDLINE_LAN_TEST_PORT", str(proxy.server_port))
    monkeypatch.setenv("HARDLINE_LAN_TEST_CA", str(certs / "ca.crt"))
    monkeypatch.setenv("HARDLINE_LAN_TEST_CERT", str(certs / "client.crt"))
    monkeypatch.setenv("HARDLINE_LAN_TEST_KEY", str(certs / "client.key"))
    assert tls.tls_request("/send", {"body": "test"}) == {"ok": True}
    assert len(backend.received) == 1
    monkeypatch.setenv("HARDLINE_LAN_TEST_CA", str(certs / "bad.crt"))
    with pytest.raises(RuntimeError, match="TLS verification failed"):
        tls.tls_request("/send", {"body": "test"})
    assert len(backend.received) == 1


def test_trusted_server_certificate_without_matching_ip_is_rejected(config, monkeypatch, certs, backend):
    fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert((certs / "client.crt").read_text())).hexdigest()
    bindings = {fingerprint: hashlib.sha256(("a" * 43).encode()).hexdigest()}
    server = create_server(certfile=certs / "client2.crt", keyfile=certs / "client2.key",
                           client_ca=certs / "ca.crt", backend_port=backend.server_port, bindings=bindings)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for name, value in {"HARDLINE_LAN_TEST_HOST": "127.0.0.1", "HARDLINE_LAN_TEST_PORT": str(server.server_port),
                            "HARDLINE_LAN_TEST_CA": str(certs / "ca.crt"),
                            "HARDLINE_LAN_TEST_CERT": str(certs / "client.crt"),
                            "HARDLINE_LAN_TEST_KEY": str(certs / "client.key")}.items():
            monkeypatch.setenv(name, value)
        with pytest.raises(RuntimeError, match="TLS verification failed"):
            tls.tls_request("/send", {"body": "test"})
        assert backend.received == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
