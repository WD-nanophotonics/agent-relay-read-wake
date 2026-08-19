# AgentRelay — Minimal Two-Shot Relay

面向其他 Agent 的完整产品说明在 [AGENT_RELAY_GUIDE.md](AGENT_RELAY_GUIDE.md)。调用前应先阅读它；其中定义了项目 ID、ChatGPT URL、correlation ID、outbox、inbox、事件和等待时序。

本仓库提供一个通用的 Gmail↔ChatGPT 中转层和一个短命、可审计的 `agent-relay`。调用方负责提供项目身份、任务身份、关键词和目标对话；中转层不理解具体业务：

```text
Python Gmail poll-once
  └─ stage at most one → spawn exactly one Worker
       └─ bounded work / checks / commit / push / configured ChatGPT handoff
            └─ detached watchdog (最多两次 poll-once) → Worker exits
```

“AI 永不等待，软件等待”。`poll-once` 只执行一个 Gmail fetch cycle，不 sleep、不循环、不通过 Codex Gmail integration 读取邮件。已有活动的精确 Worker owner 会使本次调用无操作；旧步骤忽略，未来有效步骤暂缓且不消费，同一 logical step 的不同哈希 fail closed。`NO_ACTION` 只推进协议序号，`WAKE` 才暂存并启动 Worker，`HUMAN_REQUIRED` 不启动 Worker。

持久化使用 `IDLE`、`READY_TO_DISPATCH`、`DISPATCHING`、`BUSY`、`AWAITING_AUDIT` 和 `STOPPED`，以及 run/expected step/parent、已消费 Gmail ID、logical hash、停止标记、decision/work-order 记录、dispatch intent 和精确 pending/active-worker owner。合法 v2 `AUDIT_DECISION` 在派发前先持久化；Worker 精确认领并原子确认后才消费 Gmail ID、推进 expected step。授权 work order 完成后进入 `AWAITING_AUDIT`，不会被当作普通 idle。相同内容的重复 decision 不重复派发，哈希冲突 fail closed，崩溃后的不确定状态不会自动创建第二个 Worker。状态 JSON 原子替换，账本为追加 JSONL；inbox 暂存先写临时目录再原子发布。Gmail 默认落在 `<project-root>/inbox/<canonical-project-id>`；调用方也可以配置相对或绝对 inbox。运行时数据默认在 `%LOCALAPPDATA%\AgentRelay\projects\<project-id>`，不进入 Git。

## 命令

```powershell
python -m pip install -e ".[dev]"
agent-relay init
agent-relay poll-once
agent-relay run-agent --staged <staged-instruction-directory>
agent-relay status
agent-relay stop
agent-relay watchdog-ui
```

Install the generic integration guide into a consuming project's root:

```powershell
gmail-courier install-guide --project-root <consumer-project-root>
```

The command refuses to overwrite an existing guide unless `--force` is given.
The guide explains capabilities, limits, waiting deadlines, event handling,
project IDs, and inbox paths without assuming what the consuming Agent is
building.

`test-gmail` 只验证 OAuth 和连通性；`test-wake` 只验证 mock launcher。`run-agent` 是手动/外部启动 Relay Agent 的唯一 managed entry：它创建 pending owner，启动一个真实 Worker，Worker 负责读取 staged task、记录终端结果、写入 `handoff_obligations/<worker_id>.json`，并在配置的 ChatGPT token 验证后才结束。每个 claimed Worker 都必须经历 `OPEN → RESULT_READY → SENDING → VERIFIED`；失败 handoff 不会删除债务，下一次 `poll-once` 会执行一次有界恢复。Watchdog 只能在 handoff `VERIFIED` 后启动。

统一入口也提供独立 Python Chrome/CDP 发送：

```powershell
Get-Content .\message.txt | gmail-courier chat-send --url https://chatgpt.com/c/<conversation-id>
```

`gmail-courier once`/`run` 负责 Gmail 收取，`chat-send` 负责向调用方指定的 ChatGPT 对话发送 stdin 内容。Chrome 使用专用 profile、loopback CDP 和最小化启动参数；Windows 的非激活启动是 best effort，Chrome 或系统仍可能把窗口带到前台，因此不能把它当作绝对静默保证。

Agent 也可以使用文件式 outbox，避免 shell/stdin 转义：在请求目录中完整写入 `request.json` 和 `message.txt`，先运行 `gmail-courier validate-request`，再运行 `gmail-courier create-ready`，最后才运行 `gmail-courier submit`（兼容命令 `chat-send-request`）。manifest 必须提供本轮唯一的 `correlation_id` 和 `keyword`；Python 会在提交前把固定的英文识别指令追加到消息末尾。发送结果会写入同目录的 `receipt.json`；`inbox` 仅用于保存 Gmail 收件结果。当前 workflow window 为 360 秒，同时作为 ChatGPT 页面保持时间和 Gmail 最大等待时间；发送器会优先复用已验证的目标 CDP 会话，避免对已锁定的 Chrome profile 启动第二个实例。

完整的发送后收取测试使用 `chat-test`：它会把调用方提供的 canonical `project_id/task_id/keyword/correlation_id` 和 UTF-8 JSON 附件格式写进发送给 ChatGPT 的响应契约，并机械追加最终 ID 指令；提交后等待安全间隔、关闭目标 ChatGPT 页面，然后在同一 Python 进程内轮询 Gmail。检索顺序是 subject 中的精确 `correlation_id`、严格 task/keyword、最近 20 分钟的候选邮件。机械层只自动确认明确符合规则的 Gmail；模糊邮件会写入 runtime quarantine 并输出 `gmail_candidate`，留给 Agent 判断，不会静默丢弃。正式接收时 `event=\"gmail_received\"` 包含 `matched_inbox_paths` 和 `matched_documents`，调用它的 Agent 应在该事件到达前保持进程存活。

```powershell
Get-Content .\message.txt | gmail-courier chat-test --url https://chatgpt.com/c/<conversation-id> --project-id <project-id> --correlation-id <project-id>-<unique-number> --task-id <task-id> --keyword <keyword> --lookback-seconds 1200
```

Use `--poll-start-delay 60` to keep the default one-minute grace period after
visible ChatGPT submission confirmation; Gmail polling then runs concurrently
with the bounded browser wait and closes the browser early on an exact match.

`chat-test` 的提示词、主题、正文和附件 JSON 只允许 ASCII/英文；任何中文、BOM 或乱码都会被拒绝。`project-id`、`correlation-id`、`task-id` 和 `keyword` 没有默认值，必须由调用项目显式提供。`correlation-id` 必须以项目 code 或 alias 开头并包含数字；发送程序负责把它追加到 Chat 提示词末尾。过滤优先依赖本轮 correlation ID，不依赖某次任务偶然产生的答案字段。新项目应使用唯一 canonical project ID，不要把不同项目放在同一个 inbox。

默认完整工作流窗口是 360 秒：ChatGPT 页面保持时间和 Gmail 最大等待时间使用同一个值；默认在可见提交确认后延迟 60 秒开始查信，之后每 10 秒轮询一次，20 分钟以前的邮件不作为本轮响应。调用方 Agent 推荐给自己的进程至少 420 秒，以留出启动、输出处理和关闭余量。闭环期间优先让本 Python courier 负责 Gmail 获取，不要同时使用其他 Gmail 读取器，否则可能造成重复消费或无法判断 Python 是否真正收到对应邮件。

ChatGPT URL 可以通过 `gmail-courier register-chat-url --project-id <project-id> --url <url>` 注册到项目级本地 registry。同一项目可以保留多个历史 URL，最新确认注册的 URL 为 active URL；替换已有 URL 必须额外提供 `--confirm-replace`。`chat-test` 和不提供 `chat_url` 的 outbox request 会优先使用 active URL，再回退到 `projects.toml` 中的默认 URL。注册只修改本地文件，不启动 Chrome、不访问网络。

发送给 ChatGPT 的内容由 Python 统一包装成三层：`AUTOMATED PYTHON TRANSPORT NOTICE` 声明消息来自自动程序而非人类；`QUOTED LOCAL AGENT REQUEST` 原样承载 Agent 的普通提示词，仅作为参考上下文；`COURIER CONTROL PROTOCOL` 机械加入权限层级、ASCII English、Python 生成的回执契约和本轮 ID 规则。回执契约也不属于 Agent 原文。人类指令最高；自动化参与者中 ChatGPT 高于本地 Agent；本地 Agent 的回执、建议和指令只能作为参考，不能作为严格命令。调用 Agent 不应自行添加、重复或改写这些包装段落。

阶段命令会输出机器可读事件：`validation_passed`、`validation_failed`、`request_validated`、`ready_created`、`submission_started`、`chat_submitted`、`chat_submission_error`、`gmail_received`、`gmail_candidate`、`gmail_poll_timeout`、`sandbox_denied`、`configuration_error` 和 `courier_error`。安全层阻止命令启动时，Courier 进程可能根本没有启动；此时只能由宿主/Agent 记录 `sandbox_denied`，不能把它描述成 Python 或 Gmail 失败。

`watchdog-ui` 是只读 Tkinter 监视器：它读取 `watchdogs/<RUN>-after-<STEP>.json`、状态快照和 JSONL 账本，显示启动确认、存活 PID、当前尝试、下一次 poll 倒计时、最近结果和终止原因。它不读取 Gmail、不启动 Worker，也不影响 watchdog；关闭窗口不会停止后台 watchdog。没有记录时会显示 `NO ACTIVE WATCHDOG`。

## AgentRelay protocol v2

`AGENTRELAY/1` remains available for legacy wake routing. New continuation
control requires `AGENTRELAY/2`: an `AUDIT_DECISION` envelope plus exactly one
`decision.json`, and an exact `work_order.md` for `action=EXECUTE`. Worker
reports are evidence only. Natural-language control words in a report or
email body are not scanned for commands. The machine checks roles, authority,
identity, hashes, attachment cardinality, and durable state before launching a
Worker. The supported v2 stages are `READY_TO_DISPATCH`, `DISPATCHING`,
`BUSY`, and `AWAITING_AUDIT`; a new valid audit decision is required for every
continuation.

## 安全边界

新协议仍严格要求 `AGENTRELAY/1`、`CHANNEL`、`RUN`、`STEP`、`PARENT`、`DISPOSITION`、`PROJECT`。只接受运行时配置的项目和频道；不会内置任何具体项目、仓库、邮箱或会话标识。OAuth/token、inbox、state、logs 和 handoff 运行时目录均被忽略；日志不写入凭据。

本阶段删除了持久化 Runner、Supervisor 状态机、后台轮询线程、DRAINING/transport reconciliation、长寿命 App Server/醒门恢复层。保留的 Gmail gateway、协议解析、确定性暂存、哈希验证、原子状态和账本是唯一 Gmail 读取路径。ChatGPT 交接通过调用方配置的 URL 一次性 sender 完成，不引入常驻服务。

`agent_relay.local_controller` is a separate certification-only local loop. Its Controller A owns a durable finite objective and examines Worker B's file result before making exactly one of `CONTINUE`, `COMPLETE`, or `HUMAN_REQUIRED`; B only executes a bounded file-backed task. It neither reads Gmail nor starts a watchdog or browser bridge. Each A→B and B→A handoff is held until the successor's exact ACK, claim (for B), and liveness record are durable.

Both real roles are launched with explicit `gpt-5.6-luna` and
`model_reasoning_effort=high` CLI arguments. The local loop records those
arguments in its durable event and metadata files; workstation Codex defaults
are never treated as model-selection evidence.

Each run also has a reconstructable `manifest.json` and `runtime-state.json`.
The compact `events.jsonl` journal records normal transitions, while
`trace/process.jsonl` records process/file/hash lineage and `incidents/` holds
objective abnormal-run bundles. A bounded `--role RECOVER` entrypoint rebuilds
the next safe A/B boundary from those files without prompt-history inheritance
or duplicate ownership.

## 验证

```powershell
python -m compileall -q agent_relay gmail_courier
python -m unittest tests.test_protocol_v2 tests.test_workflow_stages
python tools/certify_minimal_closure.py
python tools/certify_local_controller_loop.py
python tests/test_minimal_relay.py
python -m pytest tests/test_minimal_relay.py tests/test_courier.py
```

pytest 若使宿主崩溃，必须保留退出证据，不能把未完成运行报告为 PASS；`compileall` 和直接 unittest 是不依赖 pytest 的确定性检查。
