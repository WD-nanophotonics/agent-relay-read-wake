# AgentRelay Read & Wake Supervisor (Phase 1)

This repository contains two deliberately separate local tools:

- `gmail-courier`: the original attachment courier and Gmail/OAuth reference implementation.
- `agent-relay`: a deterministic **Read & Wake Supervisor** for one configured Codex project.

AgentRelay is not an AI agent, does not run Codex continuously, does not poll Gmail through Codex, and does not automate Chrome. **AI never waits; software waits.** Gmail polling and wake authorization are ordinary, auditable software decisions.

## Phase 1 architecture

```text
Gmail -> deterministic AGENTRELAY/1 parser -> atomic project inbox staging
      -> persistent state + JSONL audit ledger -> one mock Codex work lease
```

The desktop UI starts in `STOPPED`. Only **Start** enables polling. **Stop** immediately blocks new Gmail processing, attachment staging, and wake attempts; closing the window also stops monitoring. The UI exposes status, project/target binding, run/step, last events, and safe Gmail/mock-wake diagnostics.

States are `STOPPED`, `MONITORING`, `STAGING`, `READY_TO_WAKE`, `WAKING`, `AGENT_RUNNING`, `WAITING_FOR_REPLY`, `HUMAN_REQUIRED`, and `ERROR`. Invalid, future, or conflicting messages fail closed and never wake an agent.

## Configure and run

Install as usual, then create the local configuration:

```powershell
python -m pip install -e ".[dev]"
agent-relay init
notepad "$env:LOCALAPPDATA\AgentRelay\agentrelay.toml"
agent-relay ui
```

`agentrelay.example.toml` is a checked-in, credential-free example. It configures one project (`gmail-courier`) and its isolated runtime storage at `%LOCALAPPDATA%\AgentRelay\projects\gmail-courier`. AgentRelay reuses the existing Gmail Courier OAuth token from `%LOCALAPPDATA%\GmailCourier` by default. Do not commit OAuth clients, `token.json`, inbox contents, state, or logs.

`Test Gmail` validates authentication/connection only. `Test Wake` remains mock-only in Phase 1. The mock never starts Codex or a browser.

## Gmail protocol

The first block of a message body must be exactly versioned and machine-readable:

```text
AGENTRELAY/1

CHANNEL: AR-GMAILCOURIER-A1R7P
RUN: RUN-20260816-001
STEP: 0001
PARENT: 0000
DISPOSITION: WAKE
PROJECT: gmail-courier

Human task content follows here.
```

Supported dispositions are `WAKE`, `HUMAN_REQUIRED`, and `NO_ACTION`. A wake requires a matching project/channel, supported protocol, new Gmail ID, expected run/step/parent, successful atomic staging, and no active lease. Duplicate IDs and old steps are ignored; a future step or conflicting content for an already known logical step enters `HUMAN_REQUIRED`.

Staged content is stored as `inbox/RUN-…/STEP-…/message.txt`, `manifest.json`, and `attachments/`. The manifest records Gmail identity, protocol fields, timestamps, metadata, and hashes. `ledger/events.jsonl` is append-oriented and excludes credentials.

## Phase 2 boundary

Phase 2 supplies a real `WakeAdapter` that binds to a verified Codex thread/session, a real end-of-lease contract, and optional Chrome/ChatGPT handoff. It must not replace the deterministic protocol, state machine, staging layout, or one-active-lease invariant.

---

# Original Gmail Courier

Gmail Courier is a local, one-process attachment courier. It reads messages
sent to the configured Gmail account, routes them by project code, writes
attachments to the registered project's inbox, and optionally commits and
pushes only that delivery directory.

## First setup

From this repository's virtual environment:

```powershell
python -m venv .venv
.\\.venv\\Scripts\\python -m pip install -e ".[dev]"
Copy-Item projects.example.toml "$env:LOCALAPPDATA\\GmailCourier\\projects.toml"
gmail-courier auth --client "$env:LOCALAPPDATA\\GmailCourier\\oauth-client.json"
```

The OAuth client must be a Google Cloud Desktop application with Gmail API
enabled. The courier requests only `gmail.readonly`; credentials and runtime
state stay under `%LOCALAPPDATA%\\GmailCourier` and are never committed.

Edit `projects.toml` before starting the daemon. Each project needs a unique
`code`, an absolute Git worktree `root`, an inbox path relative to that root,
the expected branch, and its Git push policy.

## Protocol

Use this subject format:

```text
[GMAIL-COURIER][PROJECT_CODE][TASK] delivery-id
```

`TASK` may be replaced by `AUDIT`, `CONTROL`, or another uppercase type. The
message must be sent from and to the configured Gmail account and contain at
least one attachment. Unknown or ambiguous project codes are written to the
local quarantine directory and are never delivered to a project.

The legacy `[GC-BRIDGE]` subject is accepted only for a project that explicitly
lists that marker in `legacy_prefixes`.

## Commands

```powershell
gmail-courier init
gmail-courier once
gmail-courier ensure
gmail-courier status
gmail-courier stop
gmail-courier install-autostart
gmail-courier uninstall-autostart
```

`once` is useful for a smoke test. `ensure` starts one detached daemon and is
safe to call repeatedly. The daemon uses a SQLite message ledger, atomic
delivery directories, and retryable Git push state.

## Safety guarantees

- Attachments are stored as data and never executed.
- Filenames are sanitized and cannot escape the configured inbox.
- Duplicate Gmail message IDs do not create duplicate deliveries.
- A temporary directory is atomically renamed only after all attachments and
  the manifest have been written.
- Git operations fail closed on the wrong worktree or branch.
- Only the current delivery path is staged and committed; unrelated changes
  remain untouched.
- A failed push is retried without downloading or committing the delivery again.
