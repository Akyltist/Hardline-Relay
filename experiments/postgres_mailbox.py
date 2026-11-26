"""Isolated PostgreSQL mailbox experiment; not a Hardline backend.

This prototype has no production authentication, liveness, leases, notifications or job
support. Never connect it to the team's mailbox or expose it to the network.
"""

import hashlib
import re
import secrets
from uuid import UUID

import psycopg
from psycopg import sql
from experiments.postgres_sessions import proof_digest


class Mailbox:
    def __init__(self, dsn: str, schema: str):
        if not re.fullmatch(r"test_lan_[0-9a-f]{32}", schema):
            raise ValueError("only unique test_lan schemas are permitted")
        self.dsn = dsn
        self.schema = schema

    def connect(self):
        conn = psycopg.connect(self.dsn)
        if conn.execute("SELECT current_database()").fetchone()[0] != "hardline_test":
            conn.close()
            raise ValueError("only hardline_test is permitted")
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
        return conn

    def initialize(self):
        with self.connect() as conn:
            conn.execute("""CREATE TABLE agent_registry (
                name text PRIMARY KEY,
                kind text NOT NULL CHECK (kind IN ('agent', 'avatar')),
                person_id text UNIQUE,
                description text NOT NULL DEFAULT '',
                CHECK ((kind = 'agent' AND person_id IS NULL) OR
                       (kind = 'avatar' AND person_id IS NOT NULL AND length(trim(person_id)) > 0))
            )""")
            conn.execute("CREATE TABLE lanes (name text PRIMARY KEY, owner uuid NOT NULL)")
            conn.execute("""CREATE TABLE credentials (
                agent_name text PRIMARY KEY REFERENCES agent_registry(name),
                token_hash text UNIQUE NOT NULL)""")
            conn.execute("""CREATE TABLE messages (
                id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                recipient text NOT NULL, body text NOT NULL, sender text,
                created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
                acked_at timestamptz,
                acknowledged boolean NOT NULL DEFAULT false)""")

    def register_agent(self, name: str, kind: str, person_id: str | None = None,
                       description: str = "") -> bool:
        """Experiment only: callers are NOT authenticated or authorized."""
        if not name or kind not in ("agent", "avatar"):
            raise ValueError("a name and valid agent kind are required")
        if (kind == "agent" and person_id is not None) or (
            kind == "avatar" and (not person_id or person_id != person_id.strip())
        ):
            raise ValueError("only avatars have a non-empty person identifier")
        with self.connect() as conn:
            row = conn.execute("""INSERT INTO agent_registry(name, kind, person_id, description)
                VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING name""",
                               (name, kind, person_id, description)).fetchone()
            return row is not None

    def issue_credential(self, agent_name: str) -> str:
        """Provision locally; the experiment exposes no provisioning endpoint."""
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        with self.connect() as conn:
            conn.execute("INSERT INTO credentials(agent_name, token_hash) VALUES (%s, %s)",
                         (agent_name, digest))
        return token

    def identify(self, token: str) -> str | None:
        if not token or not token.isascii() or len(token) > 128:
            return None
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        with self.connect() as conn:
            row = conn.execute("SELECT agent_name FROM credentials WHERE token_hash = %s",
                               (digest,)).fetchone()
            return row[0] if row else None

    def authenticated_inbox(self, token: str, lease: tuple[UUID, UUID, int] | None = None):
        name = self.identify(token)
        if name is None:
            raise PermissionError("invalid credential")
        with self.connect() as conn:
            if lease is not None:
                self._check_lease(conn, name, lease)
            return conn.execute("""SELECT id, body, sender FROM messages
                WHERE recipient = %s AND NOT acknowledged ORDER BY id""", (name,)).fetchall()

    @staticmethod
    def _check_lease(conn, lane: str, lease: tuple[UUID, UUID, int]) -> None:
        host, session, generation, *proof = lease
        digest = proof_digest(proof[0] if proof else None)
        # Lock first, then check time: a wait for the lock may outlast the lease.
        row = conn.execute("SELECT lane FROM session_leases WHERE lane = %s FOR UPDATE",
                           (lane,)).fetchone()
        if row is None or conn.execute("""SELECT 1 FROM session_leases WHERE lane = %s
            AND host_id = %s AND session_id = %s AND generation = %s
            AND proof_hash IS NOT DISTINCT FROM %s AND lease_until > clock_timestamp()""",
            (lane, host, session, generation, digest)).fetchone() is None:
            raise PermissionError("session lease is expired or superseded")

    def authenticated_ack(self, token: str, message_id: int,
                          lease: tuple[UUID, UUID, int] | None = None) -> bool:
        name = self.identify(token)
        if name is None:
            raise PermissionError("invalid credential")
        with self.connect() as conn:
            if lease is not None:
                self._check_lease(conn, name, lease)
            # Serialize explicit ack with a consuming inbox for the same lane.
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (name,))
            if lease is not None:
                self._check_lease(conn, name, lease)
            row = conn.execute("""UPDATE messages SET acknowledged = true, acked_at = clock_timestamp()
                WHERE id = %s AND recipient = %s AND NOT acknowledged
                RETURNING id""", (message_id, name)).fetchone()
            return row is not None

    def send_to_person(self, person_id: str, body: str, sender: str | None = None,
                       lease: tuple[UUID, UUID, int] | None = None) -> int:
        """Route to the assigned avatar, even if its session is offline."""
        with self.connect() as conn:
            if lease is not None:
                if sender is None:
                    raise PermissionError("fenced send requires a sender")
                self._check_lease(conn, sender, lease)
            target = conn.execute("""SELECT name FROM agent_registry
                WHERE kind = 'avatar' AND person_id = %s""", (person_id,)).fetchone()
            if target is None:
                raise LookupError("no avatar assigned to this person")
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (target[0],))
            if lease is not None:
                self._check_lease(conn, sender, lease)
            row = conn.execute("""INSERT INTO messages(recipient, body, sender)
                VALUES (%s, %s, %s) RETURNING id""", (target[0], body, sender)).fetchone()
            return row[0]

    def claim(self, name: str, owner: UUID) -> bool:
        with self.connect() as conn:
            row = conn.execute("""INSERT INTO lanes(name, owner) VALUES (%s, %s)
                ON CONFLICT (name) DO NOTHING RETURNING name""", (name, owner)).fetchone()
            return row is not None

    def send(self, recipient: str, body: str, sender: str | None = None,
             lease: tuple[UUID, UUID, int] | None = None) -> int:
        with self.connect() as conn:
            if lease is not None:
                if sender is None:
                    raise PermissionError("fenced send requires a sender")
                self._check_lease(conn, sender, lease)
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (recipient,))
            if lease is not None:
                self._check_lease(conn, sender, lease)
            row = conn.execute("INSERT INTO messages(recipient, body, sender) VALUES (%s, %s, %s) RETURNING id",
                               (recipient, body, sender)).fetchone()
            return row[0]

    def inbox(self, name: str, owner: UUID):
        with self.connect() as conn:
            return conn.execute("""SELECT m.id, m.body FROM messages m
                JOIN lanes l ON l.name = m.recipient
                WHERE l.name = %s AND l.owner = %s AND NOT m.acknowledged
                ORDER BY m.id""", (name, owner)).fetchall()

    def ack(self, message_id: int, name: str, owner: UUID) -> bool:
        with self.connect() as conn:
            row = conn.execute("""UPDATE messages SET acknowledged = true, acked_at = clock_timestamp()
                WHERE id = %s AND recipient = %s AND NOT acknowledged
                AND EXISTS (SELECT 1 FROM lanes WHERE name = %s AND owner = %s)
                RETURNING id""", (message_id, name, name, owner)).fetchone()
            return row is not None

    def compatible_inbox(self, token: str, agent: str, unread_only: bool = True,
                         limit: int = 25, auto_ack: bool = True,
                         after_id: int = 0, receipt: str | None = None,
                         lease: tuple[UUID, UUID, int] | None = None) -> dict:
        """Subset of Hardline's inbox contract, limited to the authenticated lane.

        Not suitable for production: no durable session/fencing authorization,
        shared unqualified mailboxes, truncation, history, or push receipts.
        """
        name = self.identify(token)
        if name is None or agent not in (name, name.split(":", 1)[0]):
            raise PermissionError("invalid credential or requested mailbox")
        limit = max(1, min(limit, 25))  # keep the prototype response bounded
        after_id = max(0, after_id)
        consuming = unread_only and auto_ack
        with self.connect() as conn:
            if lease is not None:
                self._check_lease(conn, name, lease)
            if consuming:
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (name,))
                if lease is not None:
                    self._check_lease(conn, name, lease)
            rows = conn.execute("""SELECT id, sender, recipient, body, created_at, acked_at
                FROM messages WHERE recipient = %s AND id > %s
                AND (%s = false OR NOT acknowledged) ORDER BY id LIMIT %s""",
                                (name, after_id, unread_only, limit)).fetchall()
            if consuming and rows:
                ids = [row[0] for row in rows]
                updated = conn.execute("""UPDATE messages SET acknowledged = true,
                    acked_at = clock_timestamp() WHERE recipient = %s
                    AND id = ANY(%s) AND NOT acknowledged RETURNING id, acked_at""",
                                       (name, ids)).fetchall()
                stamps = dict(updated)
                rows = [(*row[:-1], stamps.get(row[0], row[-1])) for row in rows]
            remaining = conn.execute("""SELECT count(*) FROM messages
                WHERE recipient = %s AND NOT acknowledged""", (name,)).fetchone()[0]
            cursor = rows[-1][0] if rows else after_id
            more = conn.execute("""SELECT EXISTS(SELECT 1 FROM messages
                WHERE recipient = %s AND NOT acknowledged AND id > %s)""",
                                (name, cursor)).fetchone()[0]
            messages = [dict(message_id=r[0], sender=r[1], recipient=r[2], body=r[3],
                             created_at=r[4].isoformat(), acked_at=r[5].isoformat() if r[5] else None)
                        for r in rows]
            result = dict(messages=messages, truncated=0, count=len(messages),
                          remaining=remaining, more=more)
            if rows:
                result.update(first_message_id=rows[0][0], last_message_id=rows[-1][0])
            if receipt:
                result["receipt"] = "unknown"
            return result
