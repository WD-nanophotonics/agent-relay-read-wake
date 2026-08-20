# AgentRelay — Minimal Two-Shot Relay

面向其他 Agent 的完整产品说明在 [AGENT_RELAY_GUIDE.md](AGENT_RELAY_GUIDE.md)。调用前应先阅读它；其中定义了项目 ID、ChatGPT URL、correlation ID、outbox、inbox、事件和等待时序。

本仓库的正式工作流是 ChatGPT assistant → Python/CDP reader → 本地 `inbox/chatgpt`。调用方负责提供项目身份、工作单身份和目标对话；中转层不理解具体业务。Gmail 收取链路已经归档为兼容功能，保留代码和旧运行数据，但不会由正式 Chat 工作流自动启动。详见 [归档说明](docs/ARCHIVED_GMAIL_TRANSPORT.md)。

```text
local Agent
  └─ chat-send-read → fixed ChatGPT conversation
       └─ wait/probe until completed assistant response
            └─ validate once → durable inbox/chatgpt work order → local Agent/Worker
```

“AI 永不等待，软件等待”。`poll-once` 只执行一个 Gmail fetch cycle，不 sleep、不循环、不通过 Codex Gmail integration 读取邮件。已有活动的精确 Worker owner 会使本次调用无操作；旧步骤忽略，未来有效步骤暂缓且不消费，同一 logical step 的不同哈希 fail closed。`NO_ACTION` 只推进协议序号，`WAKE` 才暂存并启动 Worker，`HUMAN_REQUIRED` 不启动 Worker。

持久化使用 `IDLE`、`READY_TO_DISPATCH`、`DISPATCHING`、`BUSY`、`AWAITING_AUDIT` 和 `STOPPED`，以及 run/expected step/parent、logical hash、停止标记、decision/work-order 记录、dispatch intent 和精确 pending/active-worker owner。ChatGPT 正式回信使用 `AGENTRELAY_OUTBOUND/2`，由 Python 校验项目名、work-order ID、JSON 结构和重复消费状态；`AGENTRELAY_OUTBOUND/1` 的 SHA-256 校验仅作为旧协议兼容。授权 work order 完成后进入 `AWAITING_AUDIT`，不会被当作普通 idle。相同内容的重复 decision 不重复派发，崩溃后的不确定状态不会自动创建第二个 Worker。状态 JSON 原子替换，账本为追加 JSONL；Chat work order 暂存先写临时目录再原子发布。运行时数据默认在 `%LOCALAPPDATA%\AgentRelay\projects\<project-id>`，不进入 Git。

## 命令

```powershell
python -m pip install -e ".[dev]"
agent-relay init
agent-relay chat-read-once --project-id <project-id> --chat-url <chat-url> --work-order-id <work-order-id> --read-root <local-root>
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

`poll-once`、`test-gmail`、`gmail-courier poll` 和 `chat-test` 都属于归档 Gmail 兼容入口，不是正式 Chat→本地工作流的一部分。正式 `gmail-courier chat-send-read` 会在发送后保持同一 Chat 页面，持续 probe 到目标 assistant 回执完成、校验并写入 inbox 后才结束；`agent-relay chat-read-once` 保留为已经提交请求后的手动/分阶段读取入口。

归档兼容层仍提供独立 Python Chrome/CDP 发送：

```powershell
Get-Content .\message.txt | gmail-courier chat-send --url https://chatgpt.com/c/<conversation-id>
```

`chat-send-read` 是正式 Chat-only 闭环入口；它不会要求 ChatGPT 发 Gmail。发送成功只代表 user turn 已进入 Chat，不代表 assistant 已完成；Courier 会在同一个 CDP/Playwright 会话和同一个页面内继续轮询，直到目标 work order 的 assistant 文本稳定并通过协议校验，或者 workflow window 超时。文件式请求会先写入 `submission_started`，确认 user turn 后更新为 `waiting_for_assistant`，因此提交卡住时也有可诊断的 receipt。Chrome 使用专用 profile、loopback CDP 和最小化启动参数；Windows 的非激活启动是 best effort，因此不能把它当作绝对静默保证。旧 `chat-send`、`once`/`run` 和 Gmail 收取命令仅用于兼容或单独调查。

正式 `chat-send-read` 还支持两个相互独立的可选偏好，缺省均为 `normal`：

```powershell
Get-Content .\message.txt | gmail-courier chat-send-read --url <chat-url> --project-id <project-id> --work-order-id <unique-id> --task-difficulty hard --instruction-level detailed
```

`task-difficulty` 可为 `normal|hard|challenge`，`instruction-level` 可为 `normal|detailed|manual_book`。它们只让 Python 向 ChatGPT 传达 Agent 的偏好，不改变 Agent 原文、结构化回执或等待时限；ChatGPT 拥有最终判断权。文件式正式 operation 是 `chat-send-read`，必须提供唯一 `work_order_id`；旧归档 `chat-send` 若携带非 `normal` 模式会直接验证失败，避免静默忽略。模式值会记录到事件和 `receipt.json`，需要更长周期时 Agent 必须单独设置 `workflow_window_seconds`。

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

所有 Python-to-Chat 入口统一使用 `CHAT` payload policy。项目事实、数字、路径、commit SHA、配置细节和普通命令式文本会作为透明的引用 payload 传递；Courier 不按业务含义分类、脱敏或拦截它们，只执行 ASCII/UTF-8、URL、身份、路径和请求完整性校验。该策略不覆盖 Codex 或宿主安全审核：宿主阻止时必须报告 `sandbox_denied`，不得改名、编码、拆分、隐藏或改走其他工具。`chat_submission_error`、`chrome_error`、`network_error` 和 `configuration_error` 表示 Courier 已启动后的不同失败类型。OAuth token、密码、私钥等凭据仍不属于这个放宽范围，GitHub、仓库修改、commit 和 push 的安全边界也不改变。

阶段命令会输出机器可读事件：`validation_passed`、`validation_failed`、`request_validated`、`ready_created`、`submission_started`、`chat_submitted`、`chat_submission_error`、`chat_work_order_received`、`chat_work_order_duplicate`、`chat_repair_failed`、`gmail_received`、`gmail_candidate`、`gmail_poll_timeout`、`sandbox_denied`、`configuration_error` 和 `courier_error`。安全层阻止命令启动时，Courier 进程可能根本没有启动；此时只能由宿主/Agent 记录 `sandbox_denied`，不能把它描述成 Python 或 Gmail 失败。

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

### Read-only ChatGPT return path

`python -m agent_relay.chatgpt_read_relay --root <local-root> --project-id <project-id> --chat-url <https://chatgpt.com/c/... or /share/...> --work-order-id <work-order-id>` reads only the newest completed assistant response. Ordinary prose is ignored; only a strict `AGENTRELAY_OUTBOUND/2` envelope with matching project ID, work-order ID, and UTF-8 JSON is accepted. ChatGPT is not asked to calculate a cryptographic hash. If the newest completed response is missing the envelope or uses the wrong identity, Python makes at most one correction request; a second failure emits `chat_repair_failed`. Legacy `AGENTRELAY_OUTBOUND/1` envelopes with valid SHA-256 remain readable. Accepted work orders are written to `inbox/chatgpt/` and recorded in `chatgpt/outbound_receipts.json`; replay returns a duplicate event and changed content for an existing ID fails closed. The receive loop keeps one browser/CDP attachment for its whole bounded window, then closes the exact Courier target on success, timeout, or error; unrelated Chrome processes are not terminated.

Before Playwright attaches, Courier checks both the local CDP HTTP metadata and
the DevTools WebSocket handshake. A port whose JSON endpoint answers but whose
WebSocket is stale is treated as unhealthy, and Playwright attachment is
bounded by a 15-second timeout instead of blocking for the whole workflow.
When a submission is not visibly confirmed, the composer is cleared and the
exact configured conversation page is closed; the external Chrome process is
never terminated merely because it was reused.
Submission confirmation requires the exact payload to appear in a rendered
Chat user-message node; text that remains only in the composer or in the page's
generic `main` text is not accepted. Registered `/g/.../c/...` conversation
URLs are valid and use the same `/c/<id>` conversation identity.

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
