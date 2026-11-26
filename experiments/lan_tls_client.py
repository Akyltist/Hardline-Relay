"""Isolated LAN pilot client; not compatible with the real Hardline mailbox.

Requires mutual TLS and a separate test credential. No automatic wake or
accepted receipt is provided. The existing MCP test tools are reused only in
this dedicated process; the real Hardline server is never imported or changed.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import ssl
from urllib import error, request

from mcp.server.fastmcp import FastMCP

from experiments import mcp_loopback_client as client


_MAX_RESPONSE = 1024 * 1024
_PRIVATE_NETS = tuple(map(ipaddress.ip_network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _configuration():
    host = os.environ.get("HARDLINE_LAN_TEST_HOST", "")
    port = os.environ.get("HARDLINE_LAN_TEST_PORT", "")
    token = os.environ.get("HARDLINE_LAN_TEST_TOKEN", "")
    paths = [os.environ.get(name, "") for name in (
        "HARDLINE_LAN_TEST_CA", "HARDLINE_LAN_TEST_CERT", "HARDLINE_LAN_TEST_KEY")]
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise RuntimeError("invalid test gateway configuration") from None
    if (address.version != 4 or not (str(address) == "127.0.0.1" or any(address in net for net in _PRIVATE_NETS))
            or not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token) or not all(paths)):
        raise RuntimeError("invalid test gateway configuration")
    return str(address), int(port), token, paths


def tls_request(path: str, payload: dict, lease: int | None = None) -> dict:
    if path not in {"/whoami", "/lease/acquire", "/lease/heartbeat", "/inbox-compatible",
                    "/inbox", "/ack", "/send", "/send-person"}:
        raise RuntimeError("invalid test gateway operation")
    if path == "/inbox-compatible" and payload.get("auto_ack", True) is not False:
        raise RuntimeError("test inbox requires auto_ack=false")
    host, port, token, (ca, cert, key) = _configuration()
    try:
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=ca)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile=cert, keyfile=key)
    except (OSError, ssl.SSLError, ValueError):
        raise RuntimeError("test TLS credentials unavailable") from None
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    if lease is not None:
        headers.update({"X-Test-Host": client._HOST_ID, "X-Test-Session": client._SESSION_ID,
                        "X-Test-Generation": str(lease), "X-Test-Session-Proof": client._SESSION_PROOF})
    req = request.Request(f"https://{host}:{port}{path}",
                          data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          headers=headers, method="POST")
    opener = request.build_opener(request.ProxyHandler({}), _NoRedirect(),
                                  request.HTTPSHandler(context=context))
    try:
        with opener.open(req, timeout=5) as response:
            data = response.read(_MAX_RESPONSE + 1)
            if len(data) > _MAX_RESPONSE:
                raise RuntimeError("test gateway response too large")
            result = json.loads(data)
            if not isinstance(result, dict):
                raise RuntimeError("invalid test gateway response")
            return result
    except error.HTTPError as exc:
        if exc.code == 409 and path == "/lease/acquire":
            raise RuntimeError("test address is busy; after disconnect it can take up to 60 seconds to be released") from None
        if exc.code >= 500:
            raise RuntimeError("test gateway error; operation outcome may be unknown") from None
        raise RuntimeError(f"test gateway refused request ({exc.code})") from None
    except (error.URLError, OSError, ValueError):
        raise RuntimeError("test gateway unavailable or TLS verification failed; operation outcome may be unknown") from None


def create_test_mcp():
    """Expose only synthetic pilot tools; inbox never defaults to auto-ack."""
    mcp = FastMCP("hardline-lan-pilot")
    for tool in (client.experimental_send, client.experimental_send_to_person,
                 client.experimental_inbox, client.experimental_ack, client.ack):
        mcp.tool()(tool)

    @mcp.tool()
    def inbox(agent: str, unread_only: bool = True, limit: int = 25,
              auto_ack: bool = False, receipt: str | None = None, after_id: int = 0) -> dict:
        """Read this test credential's own mailbox; acknowledge explicitly."""
        return client.inbox(agent, unread_only, limit, auto_ack, receipt, after_id)

    return mcp


if __name__ == "__main__":
    client._request = tls_request
    create_test_mcp().run(transport="stdio")
