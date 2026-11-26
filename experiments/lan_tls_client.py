"""Isolated LAN pilot client; not compatible with the real Hardline mailbox.

Requires mutual TLS and a separate test credential. Optional local DSH wake
does not provide an accepted receipt. MCP test tools are reused only in
this dedicated process; the real Hardline server is never imported or changed.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import ipaddress
import json
import os
import re
import secrets
import ssl
from urllib import error, request

from mcp.server.fastmcp import FastMCP

from experiments import mcp_loopback_client as client
from experiments.lan_notice_loop import NoticeLoop


_MAX_RESPONSE = 1024 * 1024
_PRIVATE_NETS = tuple(map(ipaddress.ip_network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _wake_configuration():
    """Fail closed on partial or malformed optional local wake configuration."""
    url = os.environ.get("HARDLINE_PILOT_DSH_WAKE_URL")
    token = os.environ.get("HARDLINE_PILOT_DSH_WAKE_TOKEN")
    lane = os.environ.get("HARDLINE_PILOT_DSH_WAKE_LANE")
    if url is None and token is None and lane is None:
        return None
    match = re.fullmatch(r"http://127\.0\.0\.1:([0-9]{1,5})/notice", url or "")
    if (not match or not 1 <= int(match.group(1)) <= 65535
            or not re.fullmatch(r"[0-9a-f]{64}", token or "")
            or lane not in {"pilot:remote", "pilot:berlin"}):
        raise RuntimeError("invalid pilot DSH wake configuration")
    return url, token, lane


def _read_pilot_unread(after_id: int, lane: str) -> dict:
    # Uses the exact same lock, identity and lease as the MCP tools.
    return client.gateway_call("/inbox-compatible", {
        "agent": lane, "unread_only": True, "limit": 25,
        "auto_ack": False, "receipt": None, "after_id": after_id,
    })


def _post_wake(url: str, token: str) -> bool:
    """202 means host accepted followup, not a mailbox receipt or ack."""
    req = request.Request(url, data=json.dumps({"nonce": secrets.token_hex(8)}).encode(),
                          headers={"Authorization": "Bearer " + token,
                                   "Content-Type": "application/json"}, method="POST")
    opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(req, timeout=15) as response:
            if response.status == 202:
                return True
            # Unexpected successful response does not establish delivery state.
            raise RuntimeError("unknown pilot wake outcome")
    except error.HTTPError as exc:
        if 400 <= exc.code < 500:
            return False
        raise RuntimeError("unknown pilot wake outcome") from None
    except (error.URLError, OSError, ValueError):
        raise RuntimeError("unknown pilot wake outcome") from None


def _wake_lifespan(config):
    url, token, lane = config
    @asynccontextmanager
    async def lifespan(_mcp):
        stop = asyncio.Event()

        async def notify(_notice):
            return await asyncio.to_thread(_post_wake, url, token)

        loop = NoticeLoop(lambda after_id: _read_pilot_unread(after_id, lane), notify)
        task = asyncio.create_task(loop.run(stop))
        try:
            yield {}
        finally:
            stop.set()
            await task
    return lifespan


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
    wake = _wake_configuration()
    mcp = FastMCP("hardline-lan-pilot", lifespan=_wake_lifespan(wake) if wake else None)
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
