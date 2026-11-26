"""Loopback-only mTLS tests using disposable, generated test certificates."""
import http.client
import hashlib
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from experiments.lan_tls_proxy import MAX_BODY, create_server, validate_bind


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    openssl = shutil.which("openssl")
    if not openssl and os.name == "nt":
        candidate = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/mingw64/bin/openssl.exe"
        if candidate.exists():
            openssl = str(candidate)
    if not openssl:
        pytest.skip("openssl is needed to generate disposable test certificates")
    root = tmp_path_factory.mktemp("lan-tls-certs")

    def run(*args):
        subprocess.run([openssl, *args], cwd=root, check=True, capture_output=True, timeout=30)

    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=test-ca", "-keyout", "ca.key", "-out", "ca.crt")
    (root / "server-ext.cnf").write_text("subjectAltName=IP:127.0.0.1\n", encoding="ascii")
    for name in ("server", "client", "client2"):
        run("req", "-newkey", "rsa:2048", "-nodes", "-subj", f"/CN={name}", "-keyout", f"{name}.key", "-out", f"{name}.csr")
        extra = ("-extfile", "server-ext.cnf") if name == "server" else ()
        run("x509", "-req", "-days", "1", "-in", f"{name}.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial", "-out", f"{name}.crt", *extra)
    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=untrusted", "-keyout", "bad.key", "-out", "bad.crt")
    return root


@pytest.fixture
def backend():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.server.received.append((self.path, self.headers.get("Authorization"), self.rfile.read(int(self.headers["Content-Length"]))))
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.received = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture
def proxy(certs, backend):
    fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert((certs / "client.crt").read_text())).hexdigest()
    bindings = {fingerprint: hashlib.sha256(("a" * 43).encode()).hexdigest()}
    server = create_server(certfile=certs / "server.crt", keyfile=certs / "server.key", client_ca=certs / "ca.crt", backend_port=backend.server_port, bindings=bindings)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join()


def request(proxy, certs, client="client", body=b'{"mail":"private-message"}', headers=None, path="/send"):
    context = ssl.create_default_context(cafile=str(certs / "ca.crt"))
    context.check_hostname = False  # The production client must validate its configured server identity.
    if client:
        context.load_cert_chain(str(certs / f"{client}.crt"), str(certs / f"{client}.key"))
    conn = http.client.HTTPSConnection("127.0.0.1", proxy.server_port, context=context, timeout=5)
    try:
        conn.request("POST", path, body=body, headers=headers or {"Authorization": "Bearer " + "a" * 43})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def test_trusted_certificate_passes_and_no_sensitive_logs(certs, backend, proxy, capsys):
    assert request(proxy, certs) == (200, b'{"ok":true}')
    assert backend.received == [("/send", "Bearer " + "a" * 43, b'{"mail":"private-message"}')]
    captured = capsys.readouterr()
    assert "a" * 43 not in captured.err + captured.out
    assert "private-message" not in captured.err + captured.out


@pytest.mark.parametrize("client", [None, "bad"])
def test_no_or_untrusted_client_cert_rejected(certs, backend, proxy, client):
    with pytest.raises((ssl.SSLError, ConnectionError, OSError, http.client.HTTPException)):
        request(proxy, certs, client=client)
    assert backend.received == []


def test_trusted_certificate_with_wrong_token_or_unbound_cert_rejected(certs, backend, proxy):
    assert request(proxy, certs, headers={"Authorization": "Bearer " + "b" * 43})[0] == 403
    assert request(proxy, certs, client="client2")[0] == 403
    assert backend.received == []


def test_backend_unavailable(certs, backend, proxy):
    # Port zero cannot be connected; no non-loopback backend is used.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        unused_port = sock.getsockname()[1]
    proxy.backend_port = unused_port
    assert request(proxy, certs) == (502, b"backend unavailable")


def test_request_size_bound(certs, backend, proxy):
    assert request(proxy, certs, body=b"x" * (MAX_BODY + 1)) == (400, b"request rejected")
    assert not backend.received


def test_automatic_ack_rejected_even_for_direct_https_clients(certs, backend, proxy):
    assert request(proxy, certs, path="/inbox-compatible", body=b'{"agent":"test","auto_ack":true}')[0] == 400
    assert request(proxy, certs, path="/inbox-compatible", body=b'{"agent":"test"}')[0] == 400
    assert not backend.received
    assert request(proxy, certs, path="/inbox-compatible", body=b'{"agent":"test","auto_ack":false}')[0] == 200


@pytest.mark.parametrize("value", ["0.0.0.0", "localhost", "8.8.8.8", "::1", "127.0.0.2", "169.254.1.2", "10.0.0.01"])
def test_bind_rejects_wildcard_dns_public_or_ambiguous(value):
    with pytest.raises(ValueError):
        validate_bind(value)


@pytest.mark.parametrize("value", ["127.0.0.1", "192.168.1.2", "10.0.0.2", "172.16.1.2"])
def test_bind_accepts_explicit_loopback_or_private(value):
    assert validate_bind(value) == value
