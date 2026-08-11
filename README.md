# Gmail Courier

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
