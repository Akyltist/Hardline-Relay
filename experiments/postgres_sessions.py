"""Isolated lease/fencing experiment, NOT an authenticated Hardline backend.

UUIDs identify hosts/sessions; they are not credentials. Operations are safe
only for resources changed in the same PostgreSQL transaction as the lease
check. Never connect this experiment to real messages or expose it to a LAN.
"""

import hashlib
from uuid import UUID

import psycopg
from psycopg import sql


def proof_digest(secret: str | None) -> str | None:
    if secret is None:
        return None  # Only for direct test calls; the gateway requires a secret.
    if not isinstance(secret, str) or not secret.isascii() or not 32 <= len(secret) <= 128:
        raise ValueError("invalid session proof")
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


class Sessions:
    def __init__(self, dsn: str, schema: str):
        if not schema.startswith("test_lan_"):
            raise ValueError("only isolated test schemas are permitted")
        self.dsn, self.schema = dsn, schema

    def connect(self):
        conn = psycopg.connect(self.dsn)
        if conn.execute("SELECT current_database()").fetchone()[0] != "hardline_test":
            conn.close()
            raise ValueError("only hardline_test is permitted")
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
        return conn

    def initialize(self):
        with self.connect() as conn:
            conn.execute("""CREATE TABLE session_leases (
                lane text PRIMARY KEY, host_id uuid NOT NULL, session_id uuid NOT NULL,
                proof_hash text,
                generation bigint NOT NULL CHECK (generation > 0),
                lease_until timestamptz NOT NULL, test_counter bigint NOT NULL DEFAULT 0
            )""")

    @staticmethod
    def _validate(lane, host_id, session_id, ttl):
        if not isinstance(lane, str) or not lane.strip():
            raise ValueError("lane is required")
        if not isinstance(host_id, UUID) or not isinstance(session_id, UUID):
            raise ValueError("host and session must be UUIDs")
        if not isinstance(ttl, int) or isinstance(ttl, bool) or not 1 <= ttl <= 3600:
            raise ValueError("lease duration must be 1..3600 seconds")

    def acquire(self, lane: str, host_id: UUID, session_id: UUID, ttl: int,
                proof: str | None = None) -> int | None:
        """One winner; retry by same live owner keeps its fencing generation."""
        self._validate(lane, host_id, session_id, ttl)
        digest = proof_digest(proof)
        with self.connect() as conn:
            row = conn.execute("""INSERT INTO session_leases
                (lane, host_id, session_id, proof_hash, generation, lease_until)
                VALUES (%s, %s, %s, %s, 1, clock_timestamp() + %s * interval '1 second')
                ON CONFLICT (lane) DO UPDATE SET
                    generation = CASE WHEN session_leases.host_id = EXCLUDED.host_id
                        AND session_leases.session_id = EXCLUDED.session_id
                        AND session_leases.proof_hash IS NOT DISTINCT FROM EXCLUDED.proof_hash
                        AND session_leases.lease_until > clock_timestamp()
                        THEN session_leases.generation ELSE session_leases.generation + 1 END,
                    host_id = EXCLUDED.host_id, session_id = EXCLUDED.session_id, proof_hash = EXCLUDED.proof_hash,
                    lease_until = clock_timestamp() + %s * interval '1 second'
                WHERE session_leases.lease_until <= clock_timestamp()
                   OR (session_leases.host_id = EXCLUDED.host_id
                       AND session_leases.session_id = EXCLUDED.session_id
                       AND session_leases.proof_hash IS NOT DISTINCT FROM EXCLUDED.proof_hash)
                RETURNING generation""", (lane, host_id, session_id, digest, ttl, ttl)).fetchone()
            return row[0] if row else None

    def heartbeat(self, lane: str, host_id: UUID, session_id: UUID, generation: int, ttl: int,
                  proof: str | None = None) -> bool:
        self._validate(lane, host_id, session_id, ttl)
        digest = proof_digest(proof)
        with self.connect() as conn:
            # A wait for the row lock can outlast the lease; evaluate expiry
            # only after the lock has actually been obtained.
            conn.execute("SELECT lane FROM session_leases WHERE lane = %s FOR UPDATE", (lane,)).fetchone()
            row = conn.execute("""UPDATE session_leases
                SET lease_until = clock_timestamp() + %s * interval '1 second'
                WHERE lane = %s AND host_id = %s AND session_id = %s AND generation = %s
                  AND proof_hash IS NOT DISTINCT FROM %s
                  AND lease_until > clock_timestamp() RETURNING generation""",
                               (ttl, lane, host_id, session_id, generation, digest)).fetchone()
            return row is not None

    def fenced_increment(self, lane: str, host_id: UUID, session_id: UUID, generation: int) -> int | None:
        """A guarded database mutation, atomic with the ownership check."""
        with self.connect() as conn:
            conn.execute("SELECT lane FROM session_leases WHERE lane = %s FOR UPDATE", (lane,)).fetchone()
            row = conn.execute("""UPDATE session_leases SET test_counter = test_counter + 1
                WHERE lane = %s AND host_id = %s AND session_id = %s AND generation = %s
                  AND lease_until > clock_timestamp() RETURNING test_counter""",
                               (lane, host_id, session_id, generation)).fetchone()
            return row[0] if row else None
