# Isolated two-computer LAN pilot

This is a development experiment, not a replacement for Hardline. Its mailbox
is separate from the team's SQLite mailbox. Use only synthetic test accounts
and messages. A test message to `pilot:local` is not delivered to `codex:do`.
There are no real Hardline receipts, background notifications or reliable
message deduplication. The DSH wake prototype is a separate experiment.

The pilot uses the local test PostgreSQL database only, a loopback HTTP gateway,
a manually started mTLS proxy and an MCP stdio client. The database and plain
HTTP gateway are never exposed to the LAN. The proxy requires a verified client
certificate whose SHA-256 fingerprint is explicitly bound to the SHA-256 of
that agent's separate bearer token. The client verifies the test CA and the
server IP in the server certificate. Keep each private key and token private;
never send them in chat, commit them, or store them in public repository files.

## Status

The components and an actual test database have been exercised together on
loopback. No connection between two computers has yet been verified. The runner
does not provision identities, certificates, tokens or firewall rules. An
operator must provision a unique `test_lan_<32 lowercase hex>` schema in the
local `hardline_test` database, initialise the mailbox and session tables,
create synthetic agents and their distinct credentials, create certificates,
and prepare the fingerprint/token-hash binding file. The optional
`python -m experiments.lan_pilot_provision --help` describes a provisioner for
these synthetic accounts; it creates a fresh schema and a fresh directory, not
an update to existing data. Its certificates expire after one day. The CA must issue the
server certificate with the selected private IPv4 address as an IP subject
alternative name. Each test agent needs its own client certificate and token.

On Windows, the runner does not verify filesystem ACLs: before creating or
sharing any test keys, restrict them to the intended account and ensure other
users cannot alter the bindings file. Transfer the remote client's key and token
to the intended operator through an agreed private channel. Never transfer the
database password, CA private key, server private key or another agent's token.

## Runner and client

The runner requires an existing schema, explicit certificate and bindings
paths, and `HARDLINE_TEST_POSTGRES_DSN` restricted to the local test service on
`127.0.0.1:55432`, database and account `hardline_test`. PostgreSQL password may
be supplied securely through the process environment; do not paste it into a
shell transcript. The default listener is loopback. A specific private IPv4
bind requires the explicit `--allow-lan-test` flag. `--help` shows the arguments.
No listener, firewall rule or service is installed automatically.

The separate test MCP process is `python -m experiments.lan_tls_client` from
the project checkout. Set `HARDLINE_LAN_TEST_HOST`, `HARDLINE_LAN_TEST_PORT`,
`HARDLINE_LAN_TEST_CA`, `HARDLINE_LAN_TEST_CERT`, `HARDLINE_LAN_TEST_KEY` and
`HARDLINE_LAN_TEST_TOKEN` in its private process environment. Configure it as an
additional MCP connection in the host, not as a replacement for the working
Hardline server. The CA, certificate and key values are file paths, not their
contents. Use `inbox(auto_ack=false)` and acknowledge explicitly only after
reading the test message.

On Windows, `experiments/windows_lan_pilot_client.ps1` is a stdio wrapper for an
MCP host. Set its `RepoRoot`, `CredentialDirectory`, `PythonPath`, and
`ServerPort` arguments to local paths and the separately agreed port. It reads
the token from the protected client directory only at launch; it does not put
the token in its command line or write it to stdout. The checkout's Python
environment must have the MCP dependency installed. The wrapper's private
files must already have appropriate Windows ACLs on the receiving machine.

Before LAN exposure, verify the test schema, certificate-to-token bindings,
Windows ACLs, selected adapter and firewall scope. Test valid and invalid
credentials, wrong server identity, sending, reading, acknowledgement and
disconnect/reconnect. A timeout or HTTP 5xx may occur after a send or ack was
already committed: do not automatically retry and assume it was not executed.
After a test client disconnects, its address may remain leased for up to 60
seconds; a new client must wait rather than take the address from a live owner.
Neither certificate revocation/rotation, a complete request deadline, nor a
production audit trail is implemented. Stop the test listener after the pilot.
