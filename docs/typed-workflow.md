# Typed Courier workflow

Courier exposes a project-neutral request lifecycle:

```text
courier_capabilities
courier_prepare
courier_dispatch
courier_status
courier_wait
courier_recover
courier_capture_latest
courier_retry_once
```

An administrator first binds each project to one registered Chat conversation,
one absolute outbox root, allowed attachment roots, and attachment limits with
`configure-project`. Agent-facing calls cannot override the URL, Chrome
profile, browser flags, outbox, or attachment policy.

`courier_prepare` is the only supported request constructor. It owns the
request ID and directory, copies attachment bytes, emits a request-v1 manifest
and attachment attestation, and persists the tuple of project,
idempotency key, and payload SHA-256. Repeating the same tuple returns the same
request; reusing the key with different bytes fails closed.

`courier_dispatch` uses the existing durable FIFO and automatically reads the
receipt before deciding whether submission or same-request recovery is safe.
`courier_recover` accepts only post-submission recovery states and never
creates a replacement request.

`courier_capture_latest` is a read-only exact-request probe. It records an
atomic, fingerprint-bound `latest-probe.json` even when no reply is found.
`courier_retry_once` may re-enter the original immutable request only when a
fresh probe found neither its user turn nor a reply, no submission evidence or
live browser owner exists, and the request has never consumed its evidence
retry budget. It records authorization before attempting the retry. A second
evidence retry is always refused. A confirmed user turn remains recovery-only;
only the separately bounded `courier_resend_once` operation can perform a true
second submission.
