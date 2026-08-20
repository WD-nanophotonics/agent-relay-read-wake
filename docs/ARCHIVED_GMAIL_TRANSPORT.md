# Archived Gmail transport

The official return workflow is now ChatGPT assistant response → Python/CDP
reader → local inbox:

```text
local Agent
  -> gmail-courier chat-send-read
  -> fixed ChatGPT conversation
  -> agent-relay chat-read-once
  -> inbox/chatgpt/<work-order-id>-<payload-hash>
```

The Gmail receive path is archived compatibility functionality. It remains in
the repository so existing projects can read old state and perform separately
authorized legacy investigations, but it is not started by the official Chat
workflow and must not be used to decide whether a current ChatGPT round
completed.

Archived surfaces include:

- `gmail_courier.core.gmail_service`, `sync`, and `sync_until_received`;
- `gmail-courier once`, `poll`, and `chat-test`;
- `agent_relay.gmail.GoogleGmailGateway` and the Gmail polling relay;
- Gmail OAuth tokens, Gmail inboxes, Gmail candidates, and Gmail receipt files.

Do not delete or migrate existing Gmail runtime data as part of this status
change. Do not enable Gmail polling as a fallback automatically. If a legacy
project explicitly needs it, invoke the archived command and record that the
result came from the Gmail compatibility transport.

The archived code is not a security bypass and is not the authoritative return
path. New work should use `AGENTRELAY_OUTBOUND/1`, `chat-send-read`, and
`chat-read-once`.
