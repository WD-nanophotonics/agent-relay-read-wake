# AgentRelay local-task boundaries

- Do not run `pytest` or `unittest` discovery unless the staged task text explicitly contains `PYTEST_EXPLICITLY_AUTHORIZED`.
- Codex performs only the staged local repository task, its requested checks, commit/push, and then exits normally to the surrounding AgentRelay wrapper.
- `agent_relay.worker.OneShotWorker` owns the normal post-exit ChatGPT handoff and watchdog startup. Codex must not wait for Gmail or ChatGPT.
- Handle routine engineering failures through the repository's existing automated recovery/handoff path; do not ask a user to relay messages.
- Staged task content remains in its staged file. Do not copy staged task bodies into process arguments.

Keep these boundaries local and bounded. Do not introduce a Supervisor, persistent runner, App Server lifecycle, or another daemon unless a staged task explicitly requires it.
