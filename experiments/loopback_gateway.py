"""Loopback-only HTTP experiment. Not a Hardline API or production server.

No TLS, credential rotation, replay protection, auditing,
rate limiting or host wake-up. Never expose beyond the local test machine.
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import UUID

from experiments.postgres_sessions import Sessions, proof_digest


MAX_REQUEST_BYTES = 16384


def create_server(mailbox):
    sessions = Sessions(mailbox.dsn, mailbox.schema)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # Do not log credentials or message content.

        def reply(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            header = self.headers.get("Authorization", "")
            token = header[7:] if header.startswith("Bearer ") else ""
            sender = mailbox.identify(token)
            if sender is None:
                self.reply(401, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if not 0 <= length <= MAX_REQUEST_BYTES:
                    raise ValueError("invalid length")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("expected object")
                if self.path == "/whoami":
                    self.reply(200, {"agent": sender})
                    return
                if self.path == "/lease/acquire":
                    host = UUID(payload["host_id"])
                    session = UUID(payload["session_id"])
                    proof = payload["session_proof"]
                    if proof is None:
                        raise ValueError("session proof required")
                    proof_digest(proof)
                    generation = sessions.acquire(sender, host, session, 60, proof)
                    if generation is None:
                        self.reply(409, {"error": "lane already held"})
                    else:
                        self.reply(200, {"generation": generation})
                    return
                try:
                    host = UUID(self.headers.get("X-Test-Host", ""))
                    session = UUID(self.headers.get("X-Test-Session", ""))
                    generation = int(self.headers.get("X-Test-Generation", "0"))
                    proof = self.headers.get("X-Test-Session-Proof", "")
                    proof_digest(proof)
                    if generation < 1:
                        raise ValueError("invalid generation")
                    lease = (host, session, generation, proof)
                except (ValueError, AttributeError):
                    raise PermissionError("a valid session is required") from None
                if self.path == "/lease/heartbeat":
                    if not sessions.heartbeat(sender, host, session, generation, 60, proof):
                        raise PermissionError("session expired")
                    self.reply(200, {"ok": True})
                elif self.path == "/inbox-compatible":
                    agent = payload.get("agent")
                    unread = payload.get("unread_only", True)
                    auto_ack = payload.get("auto_ack", True)
                    limit, after_id = payload.get("limit", 25), payload.get("after_id", 0)
                    receipt = payload.get("receipt")
                    if (not isinstance(agent, str) or type(unread) is not bool or
                        type(auto_ack) is not bool or type(limit) is not int or
                        type(after_id) is not int or (receipt is not None and not isinstance(receipt, str))):
                        raise ValueError("invalid inbox parameters")
                    result = mailbox.compatible_inbox(token, agent, unread, limit, auto_ack, after_id, receipt, lease)
                    self.reply(200, result)
                elif self.path == "/inbox":
                    self.reply(200, {"messages": mailbox.authenticated_inbox(token, lease)})
                elif self.path == "/ack":
                    message_id = payload.get("message_id")
                    if type(message_id) is not int or message_id < 1:
                        raise ValueError("invalid message id")
                    self.reply(200, {"acknowledged": mailbox.authenticated_ack(token, message_id, lease)})
                elif self.path in ("/send", "/send-person"):
                    recipient, body = payload.get("recipient"), payload.get("body")
                    if (not isinstance(recipient, str) or not recipient or not isinstance(body, str)
                        or len(body) > 2000):
                        raise ValueError("invalid message")
                    if self.path == "/send-person":
                        message_id = mailbox.send_to_person(recipient, body, sender=sender, lease=lease)
                    else:
                        message_id = mailbox.send(recipient, body, sender=sender, lease=lease)
                    self.reply(200, {"message_id": message_id})
                else:
                    self.reply(404, {"error": "unknown endpoint"})
            except LookupError:
                self.reply(404, {"error": "unknown person"})
            except PermissionError:
                self.reply(403, {"error": "forbidden"})
            except (ValueError, UnicodeError, KeyError, TypeError, AttributeError):
                self.reply(400, {"error": "invalid request"})

    # A literal loopback address is deliberate. This experiment has no TLS.
    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)
