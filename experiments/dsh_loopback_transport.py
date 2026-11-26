"""Loopback delivery for a test DSH notice plugin; not wired to live Hardline."""

from __future__ import annotations

import http.client
import json
import re
from urllib.parse import urlsplit

from hardline_mcp import announce


class LoopbackDelivery:
    """Submit only a receipt nonce; the receiving plugin fixes all other text.

    HTTP 202 means queued, not read. A 5xx, timeout or unexpected response is
    ambiguous and retains the original receipt for a later retry.
    """

    def __init__(self, token: str, *, timeout: float = 3):
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}", token):
            raise ValueError("invalid loopback credential")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._token = token
        self.timeout = timeout

    def __call__(self, address: str, text: str) -> None:
        url = urlsplit(address)
        if (url.scheme != "http" or url.hostname != "127.0.0.1" or url.path != "/notice"
                or url.port is None or url.username or url.password or url.query or url.fragment):
            raise announce.NotSent("invalid loopback address")
        found = re.findall(r"receipt='([a-f0-9]{16})'", text)
        if len(found) != 1:
            raise announce.NotSent("invalid notice receipt")
        body = json.dumps({"nonce": found[0]}).encode("ascii")
        conn = http.client.HTTPConnection("127.0.0.1", url.port, timeout=self.timeout)
        try:
            try:
                conn.connect()
            except ConnectionRefusedError:
                raise announce.NotSent("local notice plugin is not listening") from None
            conn.request("POST", "/notice", body,
                         {"Authorization": "Bearer " + self._token, "Content-Type": "application/json"})
            response = conn.getresponse()
            response.read(4096)
            if response.status == 202:
                return
            if response.status in (400, 401, 403, 404, 405, 409, 413, 415):
                raise announce.NotSent("local notice plugin refused the request")
            raise RuntimeError("local notice plugin delivery is ambiguous")
        finally:
            conn.close()
