"""Experimental mTLS bridge to the test HTTP gateway; not production-ready.

Run only by explicitly creating and serving the returned server. Certificates,
gateway credentials, and network exposure are the caller's responsibility.
There is deliberately no automatic startup or modification of existing mail.
"""

import http.client
import hashlib
import hmac
import ipaddress
import json
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


MAX_BODY = 16_384
MAX_RESPONSE = 1_048_576
MAX_CONNECTIONS = 8
TIMEOUT = 5
PRIVATE_NETS = tuple(map(ipaddress.ip_network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
ENDPOINTS = frozenset(("/whoami", "/lease/acquire", "/lease/heartbeat", "/inbox-compatible", "/inbox", "/ack", "/send", "/send-person"))
FORWARDED = ("Authorization", "X-Test-Host", "X-Test-Session", "X-Test-Generation", "X-Test-Session-Proof")


def validate_bind(bind):
    """Accept a specific loopback or RFC1918 IPv4 address, never wildcard/DNS."""
    try:
        addr = ipaddress.IPv4Address(bind)
    except (ipaddress.AddressValueError, TypeError):
        raise ValueError("bind must be an IPv4 literal") from None
    if str(addr) != bind or not (str(addr) == "127.0.0.1" or any(addr in n for n in PRIVATE_NETS)):
        raise ValueError("bind must be 127.0.0.1 or a specific private IPv4 address")
    return str(addr)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, context, backend_port, bindings):
        self.context, self.backend_port = context, backend_port
        self.bindings = bindings
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(address, _Handler)

    def get_request(self):
        sock, address = self.socket.accept()
        sock.settimeout(TIMEOUT)
        try:
            return self.context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), address
        except Exception:
            sock.close()
            raise

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        pass  # Never log requests, TLS details, credentials, or mail content.


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # Exactly one request per connection.

    def setup(self):
        super().setup()
        self.connection.do_handshake()

    def log_message(self, format, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        # The stdlib default reflects the request line: do not echo or log it.
        self.reply(code, b"request rejected")

    def reply(self, status, body, content_type="text/plain"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_POST(self):
        lengths = self.headers.get_all("Content-Length", [])
        if (self.path not in ENDPOINTS or self.headers.get_all("Transfer-Encoding") or
                len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit()):
            self.reply(400, b"request rejected")
            return
        length = int(lengths[0])
        if length > MAX_BODY or any(len(self.headers.get_all(h, [])) > 1 for h in FORWARDED):
            self.reply(400, b"request rejected")
            return
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not token.isascii() or not 1 <= len(token) <= 128:
            self.reply(401, b"request rejected")
            return
        certificate = self.connection.getpeercert(binary_form=True)
        expected = self.server.bindings.get(hashlib.sha256(certificate).hexdigest()) if certificate else None
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        if expected is None or not hmac.compare_digest(expected, digest):
            self.reply(403, b"request rejected")
            return
        body = self.rfile.read(length)
        if len(body) != length:
            self.reply(400, b"request rejected")
            return
        if self.path == "/inbox-compatible":
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeError):
                self.reply(400, b"request rejected")
                return
            if not isinstance(payload, dict) or payload.get("auto_ack", True) is not False:
                self.reply(400, b"request rejected")
                return
        headers = {h: self.headers[h] for h in FORWARDED if h in self.headers}
        headers["Content-Type"] = "application/json"
        headers["Connection"] = "close"
        backend = http.client.HTTPConnection("127.0.0.1", self.server.backend_port, timeout=TIMEOUT)
        try:
            backend.request("POST", self.path, body=body, headers=headers)
            result = backend.getresponse()
            data = result.read(MAX_RESPONSE + 1)
            if len(data) > MAX_RESPONSE:
                self.reply(502, b"backend unavailable")
                return
            self.reply(result.status, data, "application/json")
        except (OSError, http.client.HTTPException, ValueError):
            self.reply(502, b"backend unavailable")
        finally:
            backend.close()


def create_server(*, certfile, keyfile, client_ca, backend_port, bindings, bind="127.0.0.1", port=0):
    """Create an opt-in server; backend host is fixed to local IPv4 loopback."""
    bind = validate_bind(bind)
    if type(port) is not int or not 0 <= port <= 65535 or type(backend_port) is not int or not 1 <= backend_port <= 65535:
        raise ValueError("invalid port")
    if not isinstance(bindings, dict) or not bindings or any(
        not isinstance(k, str) or not isinstance(v, str) or len(k) != 64 or len(v) != 64
        or any(c not in "0123456789abcdef" for c in k + v) for k, v in bindings.items()
    ):
        raise ValueError("certificate-to-token bindings required")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=certfile, keyfile=keyfile)
    context.load_verify_locations(cafile=client_ca)
    context.verify_mode = ssl.CERT_REQUIRED
    return _Server((bind, port), context, backend_port, dict(bindings))
