# AgentRelay architecture note (Phase 1 / Phase 3A / Phase 2J)

> **Current status:** ChatGPT assistant → Python/CDP reader → local inbox is
> the official return path. The Gmail polling architecture described below is
> retained as archived compatibility behavior; it is not the default workflow.

## Why software waits

The only loop is a bounded Gmail polling loop owned by the supervisor. It consumes no model tokens. Codex is contacted only after an explicit, persisted lease passes deterministic validation. A real wake implementation is intentionally absent from Phase 1.

## Persistence and recovery

Each project owns `state.json`, an append-only `ledger/events.jsonl`, and `inbox/`. `state.json` is atomically replaced after every material change. On restart, consumed Gmail IDs, expected sequence, logical-step hashes, and lease data survive. A malformed state file fails closed.

## Phase 2F App Server ownership

The real wake path is a single Supervisor-owned `codex app-server --stdio`
JSONL process and connection. `initialize` is correlated before the
`initialized` notification; thread listing/resume/start establishes a dedicated
worker, and every lease starts one `turn/start`. Both the matching
`turn/completed` event and the local `AGENTRELAY_COMPLETION/1` receipt are
required. Malformed JSON, EOF, active-writer conflicts, unsafe worker identity,
and missing completion are fail-closed conditions. The old writer is never
stolen or killed, and the adapter does not fall back to the CLI or Chrome.

## Phase 3A background ownership

`agent_relay.runner` is the production lifecycle boundary. It uses an atomic
exclusive ownership file plus a JSON metadata snapshot; PID alone is never
treated as ownership. The `AGENTRELAY_RUNNER/1` metadata contains only
operational identifiers and heartbeat data, not OAuth material or
completion/handoff tokens. Startup acquires ownership before creating a
Supervisor and performs one immediate poll. A heartbeat loop keeps status
observable while Gmail polls remain at the configured interval. Shutdown is
requested through a project-scoped control record and is accepted only when no
lease is active. Orphaned ownership is recoverable only when the recorded owner
PID is dead; a live owner is never stolen or killed.

The Windows launcher uses detached/no-window process creation flags and sends
stdout/stderr to the project runtime log. `start-background` is idempotent,
`status` is read-only, and `stop-background` is bounded. The existing
foreground `run` command remains available for diagnostics. AI never waits,
and no model or Chrome automation is invoked by status or lifecycle commands.

## Phase 2J completion and recovery path

While `AGENT_RUNNING`, `Supervisor.poll_once()` consumes the exact local
completion receipt before it performs any Gmail download. Completion helpers
are pure writers that validate the persisted lease, kind, and tokens; they do
not instantiate or mutate a Supervisor. Constructor reads are side-effect
free, while the production runner alone opts into explicit startup recovery.

The App Server controller retains `turn/completed` notifications by exact
`(thread_id, turn_id)`. A lease can close only after its matching terminal
event and completion receipt are present. Following a verified WORK handoff,
one exact `turn/interrupt` may be sent after a bounded grace period; the
controller then waits for the real terminal event and never fabricates one.
The interrupt is cleanup of a logically completed WORK lease, not a second
work attempt.

## Pre-endurance transport draining

Logical completion and physical transport termination are separate facts.
After an exact receipt and handoff are verified, the lease enters the
persisted `DRAINING` state. The supervisor does not poll Gmail or consume a
next message until the exact prior worker/turn is terminal, any bounded
interrupt has been issued at most once, and the owned transport reports
quiescence. A valid next WAKE therefore remains deferred in Gmail; it is not
lost and is never escalated as `HUMAN_REQUIRED` merely because transports
overlap. After quiescence, the supervisor transitions to `WAITING_FOR_REPLY`
and the normal poller continues the validated next work automatically.

## Future integration points

- `agent_relay.wake.WakeAdapter`: verified App Server wake and lease acceptance;
  the CLI adapter remains an explicit compatibility fallback.
- `CodexTarget`: official thread/session identity and optional app metadata.
- `wake_instruction`: the one template for the lease contract.
- `GoogleGmailGateway`: unchanged read-only Gmail boundary.

Phase 3A does not broaden Chrome/ChatGPT automation. A configured ChatGPT handoff
remains an externally verified report; the next Gmail step must be authored and
sent by the ChatGPT conversation, never by the background worker.

No Phase 2 component may classify natural language to decide whether a message wakes an agent.

## GmailCourier trusted transport v2

The current Gmail/Worker/handoff path has a separate control plane from task
payloads. `AGENTRELAY/1` remains a legacy transport wake. `AGENTRELAY/2`
messages carry explicit message kind, source/target roles, authority class,
decision identity, and work-order identity. Only an `AUDIT_DECISION` from the
Auditor with `WORKFLOW_CONTROL` authority can dispatch a Worker. Its
`decision.json` is strictly schema-checked against the mail envelope; an
`EXECUTE` decision additionally requires one `work_order.md`. A Worker report
is `EVIDENCE_ONLY`, and copied protocol-looking text in the report body is
never parsed as control.

The durable continuation states are `READY_TO_DISPATCH`, `DISPATCHING`,
`BUSY`, and `AWAITING_AUDIT` in addition to terminal `STOPPED`. Dispatch
intent, decision hash, work-order hash, owner, and post-completion obligation
are written before launching. Same-content duplicates are idempotent;
different content under the same decision or work-order ID fails closed. An
uncertain crash boundary does not start a second Worker. Completion with
`RETURN_FOR_AUDIT` remains durably `AWAITING_AUDIT` until a new structured
decision arrives.
