# Experimental DeepSeek Harness notice plugin

Research prototype only. It is not installed in the real DSH, and loading, custom
`hardline` source validation and presentation in the actual application have not
been verified. No existing conversations or production messages were used.

It binds IPv4 loopback only and accepts `POST /notice` with an exact
`Authorization: Bearer <secret>` header and `application/json` body containing
only `{ "nonce": "0123456789abcdef" }`. Configure `sessionId`, `agent`, `port`,
and a separate random 32-byte secret encoded as 64 lowercase hexadecimal digits.
Protect the secret from other users and logs; it grants authority to wake this
session. No network client should receive it. Configure a deliberately selected
live session; the plugin never finds or resumes a session. Requests cannot choose
the session, agent or message content. `agent` is a configured Hardline address,
not user input from a request.

`202` means `followup` returned without error, not that the agent read or
responded. `4xx` indicates a rejection before calling `followup`; `5xx` is
indeterminate and a retry could cause duplicate notices. The nonce is a correlation
identifier. Up to 1024 nonce outcomes are coalesced in memory for 24 hours;
unknown outcomes remain unknown rather than causing an immediate duplicate.
The cache is not durable across restart, so retries can still wake twice. Do not deploy until durable deduplication, safe
retry/coalescing, real notification receipts, authentication provisioning,
plugin loading, lifecycle, and source presentation have been verified.

The event is a user-role message with a custom source, not a privileged system
instruction. Mail content is never forwarded; an agent must authenticate to
Hardline and read it itself. Prompt responses can cost tokens. Source labeling
does not establish trust in the message's contents. Teardown closes the listener.
