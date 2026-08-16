# AgentRelay Phase 1 architecture note

## Why software waits

The only loop is a bounded Gmail polling loop owned by the supervisor. It consumes no model tokens. Codex is contacted only after an explicit, persisted lease passes deterministic validation. A real wake implementation is intentionally absent from Phase 1.

## Persistence and recovery

Each project owns `state.json`, an append-only `ledger/events.jsonl`, and `inbox/`. `state.json` is atomically replaced after every material change. On restart, consumed Gmail IDs, expected sequence, logical-step hashes, and lease data survive. A malformed state file fails closed.

## Future integration points

- `agent_relay.wake.WakeAdapter`: verified Codex App Server/SDK wake and lease acceptance.
- `CodexTarget`: official thread/session identity and optional app metadata.
- `wake_instruction`: the one template for the lease contract.
- `GoogleGmailGateway`: unchanged read-only Gmail boundary.

No Phase 2 component may classify natural language to decide whether a message wakes an agent.
