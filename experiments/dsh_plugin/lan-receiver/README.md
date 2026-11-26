# LAN receiver for DeepSeek Harness - experimental

This opt-in bundle receives local signals from the experimental Relay MCP client. It targets one explicitly pinned live session; it does not inspect message bodies. The full flow in a real DSH session remains unverified. See [setup instructions](../../../docs/LAN_WAKE_RU.md). Never commit keys, tokens, session identifiers or local configuration.

The loopback listener requires a separate random token stored in a private file. An HTTP 202 means only that `followup` returned successfully; it does not mean that the chat read or acknowledged any mail. Timeouts and crashes can create duplicate notices. A missing or wrong session is never replaced automatically by another session.
