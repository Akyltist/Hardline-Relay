# Experimental DeepSeek Harness notice plugin

Research prototype only. It is not installed in the real DSH, and loading, custom
`hardline` source validation and presentation in the actual application have not
been verified. No existing conversations or production messages were used.

It binds IPv4 loopback only and accepts `POST /notice` with an exact
`Authorization: Bearer <secret>` header and `application/json` body containing
only `{ "nonce": "0123456789abcdef", "agent": "codex", "route": "example-route" }`.
Configure `sessionId`, `agent`, `route`, `port`, and a separate random 32-byte
secret encoded as 64 lowercase hexadecimal digits. On the sending MCP server,
set `HARDLINE_AGENT` and `HARDLINE_DSH_WAKE_ROUTE` to the exact same base agent
and route as the receiver, plus the other `HARDLINE_DSH_WAKE_*` settings. Give
each server-to-session binding a distinct route and secret. A missing, invalid,
or mismatched route or agent is rejected before the session is woken.
Protect the secret from other users and logs; it grants authority to wake this
session. No network client should receive it. Configure a deliberately selected
live session; the plugin never finds or resumes a session. Requests cannot choose
the session, agent or message content. The agent must be the base name, not a
single lane: the server batches mail across all lanes held by its session and
the shared mailbox, and the unqualified inbox reads these together. The route
is an operator-assigned binding, not proof of a user's identity. Configuring
two sessions with the same binding or secret is unsafe. Previous versions sending
only a nonce are rejected and both ends must be upgraded together in a test.

`202` means `followup` returned without error, not that the agent read or
responded. `4xx` indicates a rejection before calling `followup`; `5xx` is
indeterminate and a retry could cause duplicate notices. The nonce is a correlation
identifier. Up to 1024 nonce outcomes are coalesced in memory for 24 hours;
unknown outcomes remain unknown rather than causing an immediate duplicate.
If `followup` throws after its outcome becomes uncertain, retries of the same
nonce return 500 until that cached outcome expires or the receiver restarts;
automatic recovery is therefore not guaranteed.
The cache is not durable across restart, so retries can still wake twice. Do not deploy until durable deduplication, safe
retry/coalescing, real notification receipts, authentication provisioning,
plugin loading, lifecycle, and source presentation have been verified.

The event is a user-role message with a custom source, not a privileged system
instruction. Mail content is never forwarded; an agent must authenticate to
Hardline and read it itself. Prompt responses can cost tokens. Source labeling
does not establish trust in the message's contents. Teardown closes the listener.
