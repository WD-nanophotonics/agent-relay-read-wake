# ChatCourier integration guide

## MePhC bridge boundary

**Normative correction:** for MePhC, the only Agent entry point is the
committed `scripts/relayctl courier` bridge. The malformed legacy sentence
immediately below is non-normative; it must not be used as an invocation
example.

For MePhC, this public CLI is called only by the committed elayctl courier bridge. Do not bypass that bridge with direct command-file, Python, Browser, Chrome, or Gmail invocations. The generic examples below describe Courier itself, not an authorization to bypass a project runtime gate.

For MePhC the exact command is scripts/relayctl courier with the existing
request directory. More generally, a project-provided Courier bridge overrides
the generic examples: use either that one bridge command or the generic
launcher sequence below, never a mixture of both.

Use this repository only for the mechanical ChatGPT transport. Prepare a
request directory, then use this exact sequence:

For cross-machine handoff, `courier_quiescence` is the only Agent-facing
readiness probe. It is read-only and must report `quiescent=true` before the
owning project releases remote workflow ownership. Never delete queue or owner
files merely to make this probe pass.

```text
<courier-root>/scripts/chat-courier.cmd validate <request-dir>
<courier-root>/scripts/chat-courier.cmd run <request-dir>
```

## Minimal correct invocation example

This is a generic example. Replace only the angle-bracket values; do not copy a
different project's URL, request ID, profile path, or runtime directory.
Assume the project URL was already registered through the two-step registration
flow below.

Create one fresh request directory in the calling project's own local outbox:

```text
<project-root>/.courier_outbox/<PROJECT_ID>/<UNIQUE_REQUEST_ID>/
  request.json
  message.txt
  attachments/                 # optional; omit from request.json for text-only work
```

`request.json` for a normal text-only request is:

```json
{
  "version": 1,
  "project_id": "<PROJECT_ID>",
  "request_id": "<UNIQUE_REQUEST_ID>",
  "message_file": "message.txt",
  "workflow_window_seconds": 600,
  "queue_wait_seconds": 3600,
  "task_difficulty": "normal",
  "instruction_level": "normal"
}
```

Write the Agent's request, evidence summary, and any existing authorized
reference/path in UTF-8 `message.txt`. Do not put a `chat_url` in ordinary
requests: Courier uses the already registered URL for `project_id`. For an
attachment, add only explicit relative paths, for example
`"attachments": ["attachments/evidence.json"]`; prefer omitting this field
for text-only work.

Run the two stages only through the official Windows command launcher. It fixes the Courier
source root, selects the approved Python, replaces any inherited `PYTHONPATH`,
and records `courier_source_root` plus a content-derived `courier_build_id` in
every JSON event. Calling Agents must not
invoke `python -m chat_courier.cli` directly or set Python/profile environment
variables themselves.

```powershell
$courier = '<courier-repository-root>/scripts/chat-courier.cmd'
$request = '<project-root>/.courier_outbox/<PROJECT_ID>/<UNIQUE_REQUEST_ID>'

& $courier validate $request
# Proceed only when event=validation_passed.
& $courier run $request
# Keep this calling Agent alive; inspect response.txt only after response_received.
```

`run` automatically joins Courier's durable FIFO queue. It does not start
Chrome while queued. `queue_joined` and repeated `queue_waiting` events are
normal progress, not transport failures; they include queue position and a
conservative wait upper bound. After `queue_turn_acquired`, Courier performs
its internal browser readiness check. The 600-second Chat response window
starts only after Courier visibly confirms the submitted user turn, so Chrome
launch, navigation, and attachment preparation do not consume it. Do not
re-run, replace, or redirect a queued request. The default queue limit is 3600
seconds and is independent of the response window. Keep the calling Agent alive
for at least `queue_wait_seconds + 600 active-setup seconds +
workflow_window_seconds + 60` seconds (4860 seconds by default).

`queue_wait_seconds` measures only time actually waiting behind a queued or
active predecessor. It never includes time from an earlier browser turn that
was interrupted before browser ownership: the same immutable request may safely
recover that turn without immediately timing out because its old ticket is aged.

The request ID is single-use. Never edit `request.json`, `message.txt`, or an
attached file after the first receipt is written. A changed request needs a new
directory and new ID.

A project bridge may create an optional immutable `retry_message_file` before
the first validation. It is included in the original request fingerprint and
may be selected only by the bounded `courier_resend_once` operation. This is a
second wording attempt for the same logical request, not a new request or an
authorization to edit either message after submission.

### Terminal-event branches

| Courier result | Correct Agent action |
| --- | --- |
| `response_received` | Read the saved `response.txt`; the transport run is complete. |
| `queue_timeout` | No browser or Send occurred. The same unchanged request can be retried when capacity is available. |
| `queue_recovery_required` | A prior active request stopped at an unsafe boundary. Do not bypass it; only its original immutable request may perform recovery. |
| `queue_duplicate_runner` | The same request is already handled by a live Courier process. Do not start another copy. |
| `courier_interrupted` | The calling environment sent Ctrl+C. `interruption_stage=pre_browser` is safe to retry once with the same unchanged request; any later stage is fail-closed. If it repeats with the same `courier_build_id` and execution host, stop and report an execution-host interruption. A verified Courier source repair with a different build ID permits one further same-request pre-browser retry; it never permits changing the request, profile, URL, or transport. Do not use backgrounding, `nohup`, `setsid`, another browser, or another transport as a workaround. |
| `submission_not_started` | Read `receipt.json` and `transport_diagnostic.json`. No Send occurred; after the browser condition is resolved, the same unchanged directory may be run again. Decide any alternative evidence strategy outside Courier. |
| `chat_submission_unconfirmed` | Treat external Send as uncertain. Do not resend or create another request ID; rerun the same unchanged directory only for Courier's read-only recovery. |
| `response_timeout` or `response_protocol_error` | Send was already confirmed. Search/read first; a project bridge may then use its single bounded resend with the pre-registered immutable retry message. |
| `chat_composer_not_ready` | The page is visible but cannot safely accept text. Report the snapshot; do not create a replacement Chat or use another transport. |
| `chat_auth_required`, `chat_access_denied`, `chat_target_mismatch`, `configuration_error`, or `browser_error` before submission | Stop and report the structured event. Do not change profile variables, create a new Chat, or route around Courier. |

`preflight` is an optional human diagnostic, not part of the Agent workflow.
When the queue is occupied it returns successful `queue_waiting` and does not
open Chrome. When it is empty, `preflight` opens only the dedicated Courier profile and registered ChatGPT
conversation. It does not fill the composer, remove a draft, upload a file,
send a message, write a request receipt, or read a response. Do not use it to
gate or duplicate `run`; the one `run` invocation performs its own readiness
check after acquiring the queue turn. Do not invoke Gmail, a second browser
controller, or a parallel reader for the same request.

The calling environment must keep its Courier child process alive for the
declared lifetime. If its own command runner sends Ctrl+C, expires, or tears
down the process tree, Courier cannot safely override that control. One
pre-browser interruption may be retried unchanged. Repeated interruption with
the same `courier_build_id` is an execution-host incident, not a reason to
change request IDs or launch methods. A documented source repair producing a
different build ID permits one additional same-request pre-browser retry.

Never set `CHAT_COURIER_PROFILE`, `AGENT_RELAY_CHATGPT_PROFILE`, or
`CHAT_COURIER_PROFILE_DIRECTORY` during ordinary Agent work. In particular,
never point Courier at `Google\\Chrome\\User Data`, `Default`, or `Profile N`.
Those variables are human-only setup settings for an explicitly prepared
dedicated Courier profile. On `chat_auth_required`, `configuration_error`, or
`browser_error`, stop without retrying, changing profile settings, using a
normal browser, or sending through another transport; report the structured
event to the human/operator.

If `run` returns `chat_submission_unconfirmed`, the browser attempted Send but
could not establish visible proof of the user turn. It writes
`submission_diagnostic.json` and, when available,
`submission_diagnostic.png` in the request directory. Treat this as an
uncertain external state: do not create another request ID or resend. Re-run
the same unchanged request directory only for Courier's read-only recovery;
it will search for the matching reply without submitting again.

`submission_not_started` means Courier failed before any Send action (for
example, the composer was not editable). The same unchanged request directory
may be run again after the browser condition is resolved. In contrast,
`chat_submission_unconfirmed` is an uncertain external-send state and remains
read-only recovery only.

For attachments, read the phase events rather than inferring the cause from a
later composer error. Courier samples the page once per second, has a 120-second
upload confirmation ceiling, and stops after 30 seconds with no observable
attachment-state change. It distinguishes `attachment_upload_failed`,
`attachment_upload_stalled`, `browser_page_unresponsive`, and
`page_closed_during_upload`. Any pre-submit terminal receipt includes
`failure_stage`, `next_action=agent_decision_required`,
`safe_to_retry_same_request=true`, and a local `transport_diagnostic.json`.
Do not ask Courier to delete the attachment, alter the message, open a new
conversation, or push material to GitHub as an automatic fallback.

Prefer a text-only Chat request whenever possible. Put large evidence files in
an already authorized remote location and send Chat a concise plain-text
summary plus the relevant existing reference/path. Courier does not upload or
push those materials for the Agent; any remote publication remains a separate,
explicitly authorized workflow.

For a request with attachments, Courier waits up to 30 seconds for a visible,
enabled Send button and never uses Enter as a fallback. If that control does
not become ready, it reports `chat_submission_unconfirmed` with the recorded
button-wait evidence; it does not guess that the text was sent.

Courier verifies the registered conversation ID after navigation, not merely
the `chatgpt.com` domain. If ChatGPT redirects to the home/new-chat page, it
returns `chat_target_mismatch` and sends nothing. If the target itself says
“You don't have access”, it returns `chat_access_denied` and sends nothing.
Neither event authorizes creating or using a one-off ChatGPT conversation.

## Chat URL registration

The registered project URL is the default and must be reused. A request may
omit `chat_url`, or repeat the already registered URL for legacy compatibility;
it may never silently select a different conversation.

To change or create a project URL, first propose it:

```text
chat-courier register --project-id <project> --url <new-url>
```

If the URL differs, Courier leaves the active registration unchanged and emits
`registration_confirmation_required` with a short-lived `confirmation_id`.
The Agent must stop, decide whether the change is genuinely authorized rather
than a workaround for an access or browser error, then explicitly confirm it:

```text
chat-courier confirm-register --project-id <project> --confirmation-id <id> --basis user_direct
```

Use `--basis prior_authorization` only when a prior explicit authorization is
available in the Agent's retained task context. Courier cannot prove the
origin of that assertion; the second command and basis are therefore recorded
as the deliberate, auditable confirmation. Never register a replacement URL
merely because the current one reports access denied. Report that failure to
the human/operator instead.

The Agent must use a unique `request_id`, keep its own process alive for at
least `queue_wait_seconds + 600 + workflow_window_seconds + 60` seconds, and
read `response.txt` only after the `response_received` event. Do not rerun a
changed request directory: create a new request ID instead.

ChatCourier's prompt marks Agent text as quoted reference. ChatGPT has final
authority over task scope, difficulty, and detail. `task_difficulty` and
`instruction_level` are optional preferences, not commands.

Run only targeted tests explicitly named for the current change. Do not add a
persistent service, mailbox transport, worker scheduler, or browser runtime
outside the single bounded ChatCourier process.

## Active project/workspace binding

The active scientific project for the current workflow is `MEPHC`, whose
canonical workspace root is `/home/icy/MePhC`. A task-specific MePhC sandbox
worktree may be used only when explicitly named; `/home/icy/TriLatt` is legacy
auxiliary state and is never the default.

For current work, create requests in the MePhC project-owned outbox and set
`PROJECT_ID=MEPHC`. Never place a current MePhC request under an outbox named
`TRILATT`, and never use a stale TriLatt request, attachment, branch, or Chat
binding as the current project context. Historical TriLatt requests are not
active work unless explicitly re-authorized and named.
