# ChatCourier

ChatCourier is a bounded Python transport for one local Agent request and one
completed ChatGPT response. It does not use Gmail, AgentRelay, a watchdog, or
a background service.

```text
request directory → Chrome → fixed ChatGPT conversation → response.txt → close
```

## Use

Register a project conversation once:

```powershell
chat-courier register --project-id EXAMPLE --url https://chatgpt.com/c/<conversation-id>
```

Prepare a request directory containing `request.json`, `message.txt`, and any
explicitly listed files under `attachments/`. Validate without side effects,
then run the bounded external operation:

```powershell
chat-courier validate .\request
chat-courier run .\request
```

```json
{
  "version": 1,
  "project_id": "EXAMPLE",
  "request_id": "EXAMPLE-001",
  "message_file": "message.txt",
  "attachments": ["attachments/context.txt"],
  "workflow_window_seconds": 360,
  "task_difficulty": "normal",
  "instruction_level": "normal"
}
```

The default window is 360 seconds. The calling Agent should remain alive for
at least 420 seconds. `run` uses one dedicated Chrome profile and one page for
send and receive, then closes both on success, timeout, or error. It never
attaches to or closes a user's normal Chrome.

ChatCourier asks ChatGPT to include `CHAT_COURIER_REPLY/1`, the project ID,
and request ID. The response body is saved verbatim as `response.txt`. ChatGPT
may choose task difficulty and instruction detail; the two optional request
fields express only the local Agent's preference.

If a run was interrupted after visible submission, rerunning the same request
directory performs a read-only recovery and does not resend the request. A
received request is idempotent. Reusing its directory with changed input is
rejected.

Chat payloads are UTF-8 and may contain ordinary project facts, paths,
numbers, and commit identifiers. Do not place credentials, tokens, passwords,
or private keys in a request. Host safety review remains outside this tool.
