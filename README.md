# ChatCourier

ChatCourier is a bounded Python transport for one local Agent request and one
completed ChatGPT response. It does not use Gmail, AgentRelay, a watchdog, or
a background service.

For a portable Windows checkout, create `.venv` in this repository and install
the package with `py -3.12 -m venv .venv` followed by
`.venv\Scripts\python.exe -m pip install -e .`. The command launcher prefers
that repository-owned environment, then an operator-configured Python, and
finally the legacy per-user Python location.

`scripts\chat-courier.cmd courier_quiescence` is a read-only handoff check. It
returns success only when the durable queue is empty and no Courier owner
record exists. It never prunes a queue, kills Chrome, sends a message, or
changes project registration.

```text
request directory → Chrome → fixed ChatGPT conversation → response.txt → close
```

## Use

Registering a project conversation is deliberately a two-step action. Propose
the URL first; this does not change an existing registration:

```powershell
chat-courier register --project-id EXAMPLE --url https://chatgpt.com/c/<conversation-id>
```

For a new or changed URL, Courier returns
`registration_confirmation_required` and a short-lived confirmation ID. The
calling Agent must make a separate, deliberate confirmation, declaring that it
has either a direct user instruction or prior explicit authorization:

```powershell
chat-courier confirm-register --project-id EXAMPLE --confirmation-id <id> --basis user_direct
```

`--basis prior_authorization` is for an authorization already present in the
Agent's retained task context. The tool records the assertion but cannot prove
its origin. Re-proposing the same already registered URL is a no-op. A request
cannot replace its project's registration via `request.json`; a different
`chat_url` is rejected. Do not register a new URL merely to work around
“You don't have access” or another browser error—report that condition.

Prepare a request directory containing `request.json`, `message.txt`, and any
explicitly listed files under `attachments/`. Agents must use the bundled
Windows command launcher, which forces the repository source root and reports the
loaded `courier_source_root` plus content-derived `courier_build_id` in every
event. The standard Agent path validates,
then runs the bounded operation:

```powershell
& .\scripts\chat-courier.cmd validate .\request
& .\scripts\chat-courier.cmd run .\request
```

`run` first joins Courier's durable FIFO queue. While it emits `queue_joined`
or `queue_waiting`, it has not opened Chrome, entered text, or sent anything.
Only `queue_turn_acquired` starts the browser portion. The independent
workflow window starts after visible submission confirmation, so browser setup
does not consume Chat's response allowance. `preflight` remains an optional human diagnostic: if the
queue is busy it emits successful `queue_waiting` without opening Chrome; if
it is empty, it opens the registered conversation using only the dedicated
Courier profile, verifies that ChatGPT is signed in and its composer is ready,
then closes. It never enters text, clears a draft, uploads files, sends a
message, writes `receipt.json`, or consumes a reply. Only `chat_ready` permits
manual diagnosis; the normal `run` repeats the required readiness check.
`chat_auth_required`, `configuration_error`, and
`browser_error` are stop states: the Agent must report them and must not retry
by changing browser/profile variables or by using another transport.

`chat_ready` means more than a visible textbox: Courier requires the composer
to be visible, enabled, editable, not currently generating, and focusable.
If that check fails, preflight emits `chat_composer_not_ready` with a local UI
snapshot instead of allowing a later `Locator.fill()` timeout. The Agent must
not create a replacement Chat conversation to work around that event.

Courier also checks that Chrome's final URL has the exact registered ChatGPT
conversation ID. A redirect to the ChatGPT home/new-chat page produces
`chat_target_mismatch`, while a visible “You don't have access” page produces
`chat_access_denied`. Both are no-send stop states and never authorize a
one-off replacement conversation.

For attachment or UI-submit failures, `chat_submission_unconfirmed` means
Courier cannot prove whether ChatGPT accepted the user turn. For a request
with attachments, Courier waits up to 30 seconds for a visible, enabled Send
button and never falls back to Enter; Enter is not reliable for attachment
drafts.
It preserves the draft and writes `submission_diagnostic.json` plus a best-
effort `submission_diagnostic.png` beside the request. This is an uncertain
state: do not create a second request or resend. Re-run the same unchanged
request directory only to perform read-only reply recovery.

By contrast, `submission_not_started` means Courier could not edit the local
composer before it attempted Send. That is a local pre-submit failure, so the
same unchanged request may be run again after the browser condition is fixed.

Attachment handling is an observable pre-submit phase. Courier emits
`attachment_upload_started` and progress events, samples the local page once
per second, and waits at most 120 seconds for visible confirmation. A healthy
page with no attachment-state change for 30 seconds is reported as
`attachment_upload_stalled`; repeated unreadable page probes are
`browser_page_unresponsive`; a closed page is `page_closed_during_upload`.
Before closing its own browser window, Courier writes
`transport_diagnostic.json` and makes a best-effort screenshot. Its terminal
receipt includes `failure_stage`, `next_action=agent_decision_required`, and
`safe_to_retry_same_request`. Courier never removes the attachment, rewrites
the message, retries automatically, creates another conversation, or pushes a
fallback copy to GitHub.

Prefer text-only requests for normal Chat work. Large evidence should live in
an already authorized remote location, while the Chat message carries a concise
plain-text summary and an existing reference/path. Courier never publishes or
pushes those materials on the caller's behalf.

```json
{
  "version": 1,
  "project_id": "EXAMPLE",
  "request_id": "EXAMPLE-001",
  "message_file": "message.txt",
  "attachments": ["attachments/context.txt"],
  "workflow_window_seconds": 600,
  "queue_wait_seconds": 3600,
  "task_difficulty": "normal",
  "instruction_level": "normal"
}
```

The default Chat workflow window is 600 seconds. An explicit
`workflow_window_seconds` in `request.json` overrides that default. The
 independent queue limit defaults to 3600 seconds and may be set with
`queue_wait_seconds` (1–7200). Courier reserves up to 600 seconds for bounded
browser setup, then gives Chat the full configured response window. The calling
Agent should remain alive for at least
`queue_wait_seconds + 600 + workflow_window_seconds + 60` seconds (4860
seconds by default). `run` uses
one dedicated Chrome profile and one page for send and receive, then closes
both on success, timeout, or error. It never attaches to or closes a user's
normal Chrome.

Queue timeout measures only time spent waiting for another queue entry. A
same-request recovery of a pre-browser interrupted turn is not rejected merely
because the original ticket is old; no browser was opened or message submitted
in that state.

`courier_interrupted` means the calling host delivered Ctrl+C to Courier. A
single `interruption_stage=pre_browser` event leaves the immutable request safe
to retry. If it repeats with the same `courier_build_id` and execution host,
stop retrying from that execution host and report the receipt instead. A
verified Courier source repair that changes `courier_build_id` permits one
additional retry of the same unchanged request. Do not evade host cancellation
with `nohup`, `setsid`, a background process, another browser, or another
transport.

The profile path is a safety boundary, not a convenience argument. Never pass
`%LOCALAPPDATA%\Google\Chrome\User Data`, one of its `Default`/`Profile N`
directories, or another interactive browser's user-data tree. Courier rejects
such paths before launching Chrome because they are locked and may expose the
user's active browser to an automation run. The Agent should use the default
dedicated profile or an explicitly prepared separate Courier profile.

Ordinary calling Agents must not set `CHAT_COURIER_PROFILE`,
`AGENT_RELAY_CHATGPT_PROFILE`, or `CHAT_COURIER_PROFILE_DIRECTORY`. These are
human/operator-only setup values. A human may use them only to select a
separate Courier-owned profile, never a profile belonging to an interactive
Chrome session.

The dedicated profile must be signed in to ChatGPT once before the first run.
Courier does not automate login or handle the Google consent screen. By
default it uses `%LOCALAPPDATA%\CodexOrchestrator\profiles\chatgpt`; set
`CHAT_COURIER_PROFILE` to an explicitly prepared Courier profile when a
different profile is required. `CHAT_COURIER_PROFILE_DIRECTORY` selects the
Chrome profile directory inside that user-data directory and defaults to
`Default`. If the selected profile shows ChatGPT's login wall, `run` stops
before filling or sending anything and records `chat_auth_required`; sign in
manually in that exact profile and retry the same request directory.

ChatCourier asks ChatGPT to include `CHAT_COURIER_REPLY/1`, the project ID,
and request ID. The response body is saved verbatim as `response.txt`. ChatGPT
may choose task difficulty and instruction detail; the two optional request
fields express only the local Agent's preference.

If a run was interrupted after visible submission, rerunning the same request
directory performs a read-only recovery and does not resend the request. A
caller may first use `courier_capture_latest` to persist exact-request presence
evidence without sending. For the narrow crash gap where the browser started
but neither the exact user turn nor a submission event exists,
`courier_retry_once` permits one fingerprint-bound retry of the unchanged
request. It requires a fresh probe, no live Courier/browser owner, no response,
and no prior evidence retry. It never changes the URL, profile, request ID, or
payload. If the user turn exists, the request remains recovery-only and this
operation refuses to resend it.
received request is idempotent. Reusing its directory with changed input is
rejected.

Inline request text is limited to 32 KiB. Larger reports must be committed and
published through the owning project's normal Git workflow; Courier receives a
compact reference bound to the published commit instead of filling the browser
composer with the report body.

Chat payloads are UTF-8 and may contain ordinary project facts, paths,
numbers, and commit identifiers. Do not place credentials, tokens, passwords,
or private keys in a request. Host safety review remains outside this tool.
