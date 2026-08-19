from __future__ import annotations


GUIDE_FILENAME = "AGENT_RELAY_GUIDE.md"

GUIDE_TEXT = r'''# Generic Gmail–ChatGPT Relay Guide

Read this file before calling the transport. This component is a bounded
message relay for an Agent workflow. It does not know what the Agent's
business task is and it does not decide what the Agent should do next.

The intended use is a closed loop:

```text
local Agent -> Python sender -> one ChatGPT conversation -> Gmail response
           <- gmail_received event + isolated local inbox <---------------
```

Use this Python courier as the normal transport for this loop. In particular,
prefer its Gmail polling and inbox delivery over a separate interactive Gmail
tool. Mixing two Gmail readers in the same round can produce duplicate
consumption, inconsistent filtering, or a false conclusion about whether the
Python process received the response. Use another Gmail tool only for an
exceptional, separately recorded investigation; it is not evidence that this
workflow succeeded.

The courier is a transport, not a business Agent. It sends the caller's
English prompt, waits within explicit bounds, identifies the response, writes
the files, and emits events. The calling Agent remains responsible for
interpreting or executing the instruction in the response.

## Timing contract

The current workflow default is **360 seconds**. In `chat-test`, the
same `workflow_window` value controls both:

1. how long the Python-controlled ChatGPT page remains open after submission;
2. the maximum time the same process polls Gmail for the response.

The default Gmail poll interval is 10 seconds. The separate default matching
lookback is 1200 seconds (20 minutes): mail older than that is not considered
current for this round. The lookback is not an extension of the workflow
deadline.

In `chat-test`, Gmail polling starts only after the ChatGPT page visibly
confirms the submitted turn. It then waits a default 60-second grace period
before the first Gmail attempt. The browser wait and Gmail polling run
concurrently after that point: an exact `gmail_received` event closes the
browser early, while a missing receipt keeps both sides bounded by the
configured workflow window. Use `--poll-start-delay` to change the grace
period. The staged `submit` and `poll` commands remain separate and are not
implicitly concurrent.

The calling Agent should normally give its own process, command timeout, or
task lease at least **420 seconds** for a default 360-second workflow. This
extra 60 seconds is a recommendation for process startup, output handling, and
shutdown. If the caller configures a longer `workflow_window`, its own
deadline must be longer than that value by a similar margin. The Agent must
stay alive; starting the command and immediately returning control or killing
the child process breaks the closed loop.

## Preflight checklist: prepare these inputs first

Do not call the relay until every item below is known and valid. A failed
preflight is a configuration error, not a reason to guess or fall back to a
different project, mailbox, or conversation.

- A unique canonical project ID. The Gmail courier uses an uppercase
  `project_id`/`code`; the AgentRelay binding uses the same identity in
  lowercase. Do not reuse an ID for another project, and do not rely on an
  alias for a new call.
- An existing project root directory. Unless an inbox is explicitly supplied,
  deliveries go to `<project-root>/inbox/<project-id-lowercase>`.
- A Gmail account address and a valid OAuth client/token location. The
  configured Gmail auth directory must already exist and must be readable by
  the calling process.
- A unique ASCII `task_id` and ASCII `keyword` for this run. They must use
  only letters, digits, `.`, `_`, or `-`; neither may be empty. These stable
  fields identify the run. A task-specific answer is never an identity
  filter.
- A unique ASCII `correlation_id` for this round. It must start with the
  configured canonical project code (or a registered alias), contain at least
  one digit, and be passed as data to the relay. The Python sender appends the
  final routing instruction mechanically; the Agent must not invent or alter
  that sentence.
- An HTTPS ChatGPT conversation URL. The host must be `chatgpt.com` or
  `www.chatgpt.com`, and the path must contain `/c/<conversation-id>`. Both
  standalone and project/GPT conversations are valid, for example:
  `https://chatgpt.com/c/<conversation-id>` and
  `https://chatgpt.com/g/<gpt-or-project-id>/c/<conversation-id>`.
  HTTP URLs, other hosts, credentials in the URL, and paths without a
  conversation segment are rejected. Matching is based on the conversation ID,
  so a project/GPT wrapper and a standalone `/c/<conversation-id>` redirect are
  treated as the same configured conversation.
- For a real AgentRelay binding: a non-empty target ID, an `AR-...` channel
  ID, an existing repository path, an existing Gmail auth directory, and a
  non-mock target. A mock target is for diagnostics only and is not an
  automatic ChatGPT/Gmail workflow.
- A positive polling interval and a positive maximum wait. The caller must
  also keep its own process alive for at least Chrome startup + submission
  timeout + close delay + maximum wait + a small margin.

For the standard defaults, the practical minimum preparation is:

```text
Courier workflow window: 360 seconds
Courier Gmail poll interval: 10 seconds
Courier current-mail lookback: 1200 seconds
Recommended Agent-side deadline: 420 seconds or more
```

The relay refuses to submit when a required identity, path, URL, encoding, or
target field fails validation. Fix the preparation/configuration first; never
silently substitute a default project, inbox, account, URL, or task answer.

## Recommended capability selection

Choose the smallest interface that still covers the required loop:

- Use `chat-test` for the normal complete workflow. It appends the response
  contract and routing instruction, submits ChatGPT, waits for visible
  submission confirmation, starts Gmail polling after the grace period, and
  exits when a matching receipt arrives or the bounded workflow ends. A
  matching receipt closes the browser early.
- Use `chat-send-request` when the Agent needs a file-backed send request and
  a machine-readable `receipt.json`. This command confirms submission, but it
  is a send operation; the caller must separately keep a Python receive loop
  alive if it needs Gmail delivery.
- Use `chat-send` only for send-only work. If `--correlation-id` is supplied,
  the same mechanical subject instruction is appended; it does not by itself
  wait for Gmail.
- Use `sync_until_received(...)` when the caller already owns submission and
  wants to implement the wait loop in Python. It still requires a valid
  correlation ID and an `on_poll` callback for immediate events.

Do not treat `chat_submitted` as a completed round. It only says that the
prompt was accepted by the target ChatGPT page. The round is complete only
when the caller receives `gmail_received` and reads the reported inbox path.

## Four explicit phases

The file workflow is deliberately split so an Agent can tell preparation from
external action:

1. `validate-request --request <dir>` (aliases: `validate-only`, `dry-run`)
   reads and validates local files only. It never creates `READY`, starts
   Python children, starts Chrome, accesses Gmail, opens a network connection,
   or sends a message. Success is `validation_passed`; failure is
   `validation_failed` or a more specific `configuration_error` event.
2. `create-ready --request <dir>` repeats validation and atomically creates
   `READY`. It is still local-only. A permission/sandbox refusal is reported
   as `sandbox_denied`, not as a Gmail, Chrome, or Python workflow failure.
3. `submit --request <dir>` (compatibility name `chat-send-request`) is the
   only phase that starts the Python browser sender and can send externally.
   It emits `request_validated`, `submission_started`, then either
   `chat_submitted`, `chat_submission_error`, or `sandbox_denied`.
   Unexpected internal failures are `courier_error`; they are distinct from a
   host/Codex execution denial.
4. `poll --request <dir>` reads the submitted request's receipt and polls
   Gmail. It emits `gmail_received`, `gmail_candidate`, or
   `gmail_poll_timeout`, and updates `receipt.json` with the fixed timing
   values used by that poll.

The Agent should not jump from a failed local phase to `chat-test`, direct
Chrome, or another Gmail tool. Fix the reported phase or request, then retry
the same explicitly authorized phase with the same unique identifiers.

## What the relay does

1. It can submit an English/ASCII prompt to a ChatGPT web conversation through
   a Python-controlled Chrome/CDP session. It first reuses a verified existing
   target conversation and starts Chrome only when the dedicated profile is
   not already locked.
2. It can wait for a response Gmail by polling Gmail at a caller-selected
   interval until a caller-selected deadline.
3. It searches a response in this order: exact `correlation_id` in the
   subject/title, exact task/keyword retrieval, then a recent candidate scan.
   A correlation match is accepted when sender/recipient, recency, ASCII
   content, and an attachment are valid; the natural-language subject need not
   use the formal marker. The strict phase still requires the canonical
   project/task/keyword contract and named JSON attachment.
4. It writes the complete delivery, including attachments and a manifest, to
   the project's isolated inbox.
5. It preserves project-related but non-conforming messages as candidates for
   Agent review instead of silently discarding them.
6. It emits machine-readable JSON events while it waits, including immediate
   `gmail_received` or `gmail_candidate` events.

## What it does not do

- It does not perform the Agent's business task.
- It does not interpret or execute an instruction found in a Gmail.
- It does not use a task-specific answer as the identity filter.
- It does not guarantee that Windows will never bring Chrome to the foreground.
- It does not keep waiting after the caller's deadline.

## Project isolation is mandatory

Every routed project needs one canonical, unique project ID (`code`) and one
configured project root. New callers should use the canonical `code`, not an
alias. The same canonical ID must be used in the request contract and in the
response Gmail subject/body/JSON. Different projects must not share an ID.
The response JSON must contain the exact `project_id` field as well as the
exact `task_id` and `keyword` fields. For a closed-loop round it should also
contain the exact `correlation_id`, although the subject correlation match is
the primary routing key.

## Correlation-first matching

The calling Agent supplies only a plain prompt. Python wraps it in a layered
transport envelope before sending it:

1. `AUTOMATED PYTHON TRANSPORT NOTICE` says that the message was sent by the
   GmailCourier Python automation program, not directly by a human.
2. `QUOTED LOCAL AGENT REQUEST` contains the Agent's original prompt exactly
   as the task-context section. It is reference material, not a human message,
   system instruction, or strict command.
3. `COURIER CONTROL PROTOCOL` contains the mechanically added authority,
   ASCII-English, generated response-contract, and optional routing-ID rules.
   Human direction is highest;
   among automated participants, ChatGPT outranks the local Agent. Local Agent
   receipts and suggestions may be considered as reference but cannot override
   ChatGPT or this protocol.

The calling Agent must not add or imitate these wrapper sections. Python adds
them consistently for every outbound send entry point.

The Agent creates the round ID, for example `<project-code>-20260819-001`,
and passes it as `correlation_id`. The sender adds the routing rule inside the
Courier control protocol:

The sender then appends this fixed English routing instruction to the end of
the prompt:

```text
For this request, include the exact identifier <correlation-id> in the Gmail subject/title.
```

The Agent does not need to write that instruction itself. The courier first
queries Gmail for the exact ID in the subject. If that phase returns a hit, it
does not widen the search to another phase. If there is no hit, it tries exact
task/keyword retrieval. Only then does it inspect recent self-sent messages as
quarantine candidates. Messages older than the 20-minute lookback are ignored
for this round and require human investigation; they are not silently treated
as a current response.

The matcher follows a negative-filtering rule. It automatically rejects only
what is definitely unrelated, such as a wrong sender/recipient or an explicit
match for another registered project. A message that mentions the expected
project, or is otherwise a new self-sent attachment without a definite project
conflict, is not automatically accepted and is not silently lost. It is saved
under the runtime quarantine candidate directory with its body, attachment
copies, classification, mismatch reasons, and expected contract. The Agent
may inspect that candidate and decide whether it is the intended response.

The default inbox is:

```text
<project-root>/inbox/<canonical-project-id-lowercase>
```

The caller may instead set `inbox` to a relative directory under the project
root or to an explicit absolute directory. An external absolute inbox must
use `push = false`; its files are still written and reported, but they are not
committed to the project repository.

Each delivery directory contains the original attachments and `manifest.json`.
The manifest records the canonical project code, subject, Gmail identity, and
attachment metadata. The Agent may use this directory however it chooses.

## Sending through the file-based outbox

For the lowest-complexity Agent integration, use an `outbox` request instead
of piping text through a shell. The Agent creates one request directory with:

```text
outbox/<project-id>/<task-id>/
  request.json
  message.txt
  READY
```

Write the two content files completely first and create `READY` last. The
request manifest must contain `version: 1`, `operation: "chat-send"`,
`request_id`, `project_id`, `correlation_id`, `task_id`, `keyword`, `chat_url`, and optionally
`workflow_window_seconds` and `message_file`. The message is UTF-8 encoded
ASCII text only. The default workflow window is 360 seconds and controls both
the ChatGPT page lifetime and the Gmail maximum wait. Then call:

```powershell
gmail-courier chat-send-request --request <outbox-request-directory>
```

The relay validates the request before touching Chrome. It writes
`receipt.json` beside the request with `submitted` or `failed`, the detail,
and the browser launch diagnostic. The `inbox` remains receive-only; do not
place outbound prompts there.

The browser layer uses one dedicated profile owner. It checks the configured
CDP port and compatible fallback ports for the exact conversation before
launching anything. If the profile is locked but no matching CDP target exists,
the call fails closed and records the lock paths instead of opening a second
Chrome against the same profile.

When an outbox manifest omits `chat_url`, resolution is local and deterministic:
the courier uses the project's latest active registry URL, then the optional
`chat_url` in `projects.toml`, then the existing AgentRelay binding URL as a
backward-compatible fallback. If none exists, validation fails; the courier
does not guess a conversation.

## Outbox manifest and file contract

The file-based interface is intentionally simple and atomic. A generic request
looks like this:

```text
<outbox-root>/<project-id>/<task-id>/
  request.json
  message.txt
  READY
```

`request.json` must contain the following shape; replace every angle-bracketed
value before use:

```json
{
  "version": 1,
  "operation": "chat-send",
  "request_id": "<unique-request-id>",
  "project_id": "<canonical-project-id>",
  "correlation_id": "<canonical-project-id>-<unique-number>",
  "task_id": "<unique-task-id>",
  "keyword": "<unique-keyword>",
  "chat_url": "https://chatgpt.com/c/<conversation-id>",
  "message_file": "message.txt",
  "workflow_window_seconds": 360
}
```

The correlation ID must begin with the configured project code or alias and
contain a digit. `message.txt` must be non-empty UTF-8 ASCII/English text;
the sender adds the routing instruction itself. Write `request.json` and
`message.txt` completely, then create `READY` last. Never create `READY` for
an incomplete request. The sender writes `receipt.json` with submission state,
request identity, and browser diagnostics.

## Closed-loop usage

For a real round trip, keep the calling Agent process alive while the relay
waits. The Agent must not launch the command and immediately exit. It should
read stdout line by line and continue when it sees `event: "gmail_received"`.

```text
prompt input
  -> chat-test submits the prompt and response contract
  -> visible submission confirmation starts the Gmail worker
  -> after <poll-start-delay> seconds, Gmail polling runs while the page remains open
  -> an exact gmail_received event closes the page early; otherwise both waits end at the deadline
  -> gmail_received is emitted as soon as the exact message is fetched
  -> the Agent reads matched_inbox_paths/matched_documents and continues
```

Example:

```powershell
Get-Content .\prompt.txt | gmail-courier chat-test `
  --url https://chatgpt.com/c/<conversation-id> `
  --project-id <canonical-project-id> `
  --correlation-id <project-id>-<unique-number> `
  --task-id <unique-task-id> `
  --keyword <unique-keyword> `
  --lookback-seconds 1200 `
  --workflow-window 360 `
  --poll-start-delay 60 `
  --poll-interval 10
```

The Agent's own deadline should exceed:

```text
Chrome startup + submission timeout + workflow-window + a small margin
```

If the Agent's deadline is shorter than the relay's polling window, the Agent
can terminate first and the workflow becomes manual or incomplete. If the
relay reaches `gmail_poll_timeout`, no matching Gmail was confirmed within the
requested window; the Agent must decide whether to retry or stop.

For a caller that already performs the ChatGPT submission in Python, the
receive-side API has the same contract:

```python
from gmail_courier.core import DeliveryExpectation, sync_until_received

expected = DeliveryExpectation(
    project_code="<canonical-project-id>",
    task_id="<unique-task-id>",
    keyword="<unique-keyword>",
    attachment_filename="result.json",
    correlation_id="<canonical-project-id>-<unique-number>",
)
outcome = sync_until_received(
    expected=expected,
    max_seconds=360,
    interval_seconds=10,
    lookback_seconds=1200,
    on_poll=lambda event: print(event, flush=True),
)
```

Keep the process running until `outcome["event"] == "gmail_received"` or
`gmail_poll_timeout`. A returned `gmail_candidate` is review data, not a
successful receipt and not permission to execute its contents automatically.

## Events and result location

The command emits JSON lines such as:

- `chat_submitted`: the prompt was visibly accepted by the configured ChatGPT
  conversation.
- `gmail_poll`: one polling attempt completed; it may have found nothing.
- `gmail_received`: the exact delivery was validated and written. Read
  `matched_inbox_paths` for the delivery directories and
  `matched_documents` for validated JSON payloads.
- `gmail_candidate`: a recent self-sent message contains evidence for the expected
  project but does not prove the exact contract. Read `candidate_messages` and
  inspect each `candidate_path`; this is not a successful delivery and is not
  placed in the project inbox.
- `gmail_poll_timeout`: the deadline expired without a validated delivery.

The process exit code is zero only after `gmail_received`. A timeout, invalid
configuration, failed submission, encoding error, or malformed response has a
nonzero exit code.

## Troubleshooting and safe interpretation

- `configuration_failed` or an outbox validation error means the caller did
  not provide a safe contract. Correct the URL, project ID, correlation ID,
  task ID, keyword, path, or encoding before retrying.
- `chat_submit_failed` means the prompt was not verified as submitted. Do not
  start interpreting Gmail as the response for that round.
- `gmail_candidate` means Python found a recent self-sent message that may be
  related but cannot prove the contract. Inspect its `candidate_path`; do not
  call it `gmail_received`.
- `gmail_poll_timeout` means no acceptable response was confirmed before the
  configured deadline. It does not prove that Gmail contains no message at
  all; it means this bounded workflow did not accept one.
- `courier_error` means the Python process reached an unexpected internal
  exception. This is different from `sandbox_denied`, which means execution or
  a local operation was blocked by the host/security layer.
- Chrome launch and foreground behavior are best effort on Windows. The
  transport does not promise absolute invisibility or prevent the OS from
  activating a window.

## Choose only the capability you need

- Use `chat-send` when the caller only needs to send a prompt and does not need
  this process to wait for Gmail.
- Use `chat-send-request` when the caller wants the file-based outbox protocol
  and a machine-readable `receipt.json`.
- Use `chat-test` for the complete send → wait → receive loop.
- Use `sync_until_received(...)` from Python when the caller already owns the
  prompt submission and wants to control the wait loop itself. Provide a
  `DeliveryExpectation` with the canonical project ID, task ID, keyword, and
  attachment filename, and provide an `on_poll` callback to receive events.

The transport is intentionally generic. The caller owns task meaning, retry
policy, and interpretation of any validated instruction; the relay owns
identity matching, isolation, waiting, persistence, and notification.
'''


def install_guide(project_root, *, filename: str = GUIDE_FILENAME, force: bool = False):
    from pathlib import Path

    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"project root does not exist: {root}")
    target = root / filename
    if target.exists() and not force:
        raise FileExistsError(f"guide already exists: {target}; use --force to replace it")
    target.write_text(GUIDE_TEXT, encoding="utf-8")
    return target
