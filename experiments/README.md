# PostgreSQL mailbox experiment

This is an isolated proof of concurrent database operations, not a replacement
for Hardline. It exercises independent connections, atomic lane claims,
addressed messages and acknowledgement. It deliberately does not implement
production authentication or authorization, liveness, delivery notifications,
recovery, jobs, migration or compatibility with the Hardline MCP API. A UUID
in the prototype is merely a test value, not a security credential. Do not
expose this code or its test database to a network or use it for real mail.

An experimental registry distinguishes agents from avatars and routes a task
addressed to a person identifier to that person's uniquely assigned avatar,
including when the avatar session is offline. Person identifiers are opaque;
the example test IDs are not CRM identifiers. Registration is entirely
unauthenticated in this prototype: it does not prove a right to represent a
person. Real deployment needs authenticated assignment and reassignment,
identity verification, authorization, an audit trail and user-visible control.

`loopback_gateway.py` tests HTTP delivery with per-agent test credentials held
only as hashes in PostgreSQL. It binds strictly to 127.0.0.1 on an ephemeral
port. This is not a deployable network server: it lacks TLS, credential
lifecycle, replay protection, audit logging, rate limiting and wake
delivery. Database credentials must remain exclusively on a trusted server;
clients must not receive direct database access. Test credentials and HTTP do
not establish an individual's identity or right to represent them.

`mcp_loopback_client.py` supplies four experimental MCP tools and a limited
`inbox`/`ack` subset over stdio, backed by the loopback gateway. The latter
exercises non-consuming reads, pagination and atomic auto-ack, but is not the
full Hardline API: there are no shared mailboxes, histories, receipt acceptance,
full Hardline session registration, automatic notification or production identity checks.
Only the caller's lane is readable, and the page size is capped at 25.
Never attach this adapter to the team's chats or expose its gateway over LAN.

`postgres_sessions.py` explores a lease and a monotonically increasing fencing
generation for a host/session. The loopback gateway requires a valid lease for
mail reads, sends and acknowledgements, checked within their database
transaction. The gateway requires a separate session proof and stores its hash;
it travels in plaintext on loopback and has not undergone a security review.
Agent credential validation and operations are separate transactions,
so future credential revocation requires an atomic design. The MCP experiment
renews its lease on each call but does not heartbeat while idle. UUIDs are not
authentication. One session may hold multiple lanes, but row deletion resets
the counter, and side effects outside the fenced transaction are not protected.
These gaps must be resolved before implementation or migration.

To run the optional tests, install psycopg 3 and pytest in an isolated Python
environment and set `HARDLINE_TEST_POSTGRES_DSN` and `PGPASSWORD`. The target
database must be named `hardline_test`. Tests create and drop only a uniquely
named temporary schema; they must never point at a real mailbox or server.

Next work: design authenticated network access and host/session identity;
preserve transactional ownership, delivery, jobs and takeover semantics; adapt
the real storage layer and notification transport; run cross-machine and
recovery tests; plan a tested migration and rollback before switching clients.
