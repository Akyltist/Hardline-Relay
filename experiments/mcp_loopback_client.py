"""Experimental MCP-to-loopback adapter; it is not a Hardline replacement.

Only a loopback gateway is supported. It deliberately omits full Hardline
registration, history, accepted receipts and push/wake delivery.
"""

import json
import os
import secrets
from threading import RLock
from urllib import error, request
from uuid import uuid4

from mcp.server.fastmcp import FastMCP


mcp = FastMCP("hardline-lan-experiment")
_HOST_ID = str(uuid4())
_SESSION_ID = str(uuid4())
_SESSION_PROOF = secrets.token_urlsafe(32)
_LOCK = RLock()


def _request(path: str, payload: dict, lease: int | None = None) -> dict:
    token = os.environ.get("HARDLINE_LAN_TEST_TOKEN", "")
    port = os.environ.get("HARDLINE_LAN_TEST_PORT", "")
    if not token or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise RuntimeError("test gateway token or port not configured")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    if lease is not None:
        headers.update({"X-Test-Host": _HOST_ID, "X-Test-Session": _SESSION_ID,
                        "X-Test-Generation": str(lease), "X-Test-Session-Proof": _SESSION_PROOF})
    req = request.Request(
        f"http://127.0.0.1:{int(port)}{path}",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=5) as response:
            return json.load(response)
    except error.HTTPError as exc:
        raise RuntimeError(f"test gateway refused request ({exc.code})") from None
    except error.URLError:
        raise RuntimeError("test gateway unavailable") from None


def gateway_call(path: str, payload: dict) -> dict:
    # Renew on each call. This test client does not heartbeat while idle.
    with _LOCK:
        lease = _request("/lease/acquire", {"host_id": _HOST_ID, "session_id": _SESSION_ID,
                                            "session_proof": _SESSION_PROOF})
        return _request(path, payload, lease["generation"])


@mcp.tool()
def experimental_send(to_agent: str, message: str) -> dict:
    """Send test mail from the credential's agent, without a caller-supplied sender."""
    return gateway_call("/send", {"recipient": to_agent, "body": message})


@mcp.tool()
def experimental_send_to_person(person_id: str, message: str) -> dict:
    """Route test mail by an opaque person ID to its configured avatar."""
    return gateway_call("/send-person", {"recipient": person_id, "body": message})


@mcp.tool()
def experimental_inbox() -> dict:
    """Read unread test messages for this credential without acknowledging them."""
    return gateway_call("/inbox", {})


@mcp.tool()
def experimental_ack(message_id: int) -> dict:
    """Acknowledge a test message only when addressed to this credential."""
    return gateway_call("/ack", {"message_id": message_id})


@mcp.tool()
def inbox(agent: str, unread_only: bool = True, limit: int = 25,
          auto_ack: bool = True, receipt: str | None = None, after_id: int = 0) -> dict:
    """Experimental subset of Hardline inbox; only this credential's lane.

    Shared mailboxes, histories, receipt confirmation, sessions and push are
    not supported. The page size is capped at 25 during this experiment.
    """
    return gateway_call("/inbox-compatible", {"agent": agent, "unread_only": unread_only,
                        "limit": limit, "auto_ack": auto_ack, "receipt": receipt, "after_id": after_id})


@mcp.tool()
def ack(message_id: int) -> dict:
    """Experimental single-message acknowledgement for this credential's lane."""
    return {"ok": gateway_call("/ack", {"message_id": message_id})["acknowledged"]}


if __name__ == "__main__":
    mcp.run(transport="stdio")
