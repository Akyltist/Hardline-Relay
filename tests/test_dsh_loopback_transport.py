"""Tests use only an ephemeral fake local server and never touch real mail."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from experiments.dsh_loopback_transport import LoopbackDelivery
from hardline_mcp import announce


@pytest.fixture
def server():
    events = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            events.append(json.loads(body))
            self.send_response(self.server.test_status)
            self.end_headers()

    host = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    host.test_status = 202
    thread = Thread(target=host.serve_forever, daemon=True)
    thread.start()
    try:
        yield host, events
    finally:
        host.shutdown()
        host.server_close()
        thread.join(timeout=5)


def test_notice_sends_only_nonce_and_distinguishes_outcomes(server):
    host, events = server
    delivery = LoopbackDelivery("a" * 64)
    address = f"http://127.0.0.1:{host.server_port}/notice"
    delivery(address, "receipt='0123456789abcdef' ignored private message")
    assert events == [{"nonce": "0123456789abcdef"}]
    host.test_status = 401
    with pytest.raises(announce.NotSent):
        delivery(address, "receipt='0123456789abcdef'")
    host.test_status = 500
    with pytest.raises(RuntimeError):
        delivery(address, "receipt='0123456789abcdef'")


def test_wrong_destination_or_notice_is_not_sent(server):
    host, events = server
    delivery = LoopbackDelivery("a" * 64)
    for address, text in (("http://example.com:80/notice", "receipt='0123456789abcdef'"),
                          (f"http://127.0.0.1:{host.server_port}/other", "receipt='0123456789abcdef'"),
                          (f"http://127.0.0.1:{host.server_port}/notice", "no receipt")):
        with pytest.raises(announce.NotSent):
            delivery(address, text)
    assert events == []
