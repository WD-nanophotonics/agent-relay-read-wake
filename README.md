# AgentRelay — Minimal Two-Shot Relay

本仓库保留 `gmail-courier` 作为 Gmail/OAuth 参考实现，并提供一个短命、可审计的 `agent-relay`。生产路径不是常驻 Supervisor：

```text
Python Gmail poll-once
  └─ stage at most one → spawn exactly one Worker
       └─ bounded work / checks / commit / push / fixed ChatGPT handoff
            └─ detached watchdog (最多两次 poll-once) → Worker exits
```

“AI 永不等待，软件等待”。`poll-once` 只执行一个 Gmail fetch cycle，不 sleep、不循环、不通过 Codex Gmail integration 读取邮件。已有活动的精确 Worker owner 会使本次调用无操作；旧步骤忽略，未来有效步骤暂缓且不消费，同一 logical step 的不同哈希 fail closed。`NO_ACTION` 只推进协议序号，`WAKE` 才暂存并启动 Worker，`HUMAN_REQUIRED` 不启动 Worker。

持久化只有 `IDLE`、`BUSY`、`STOPPED` 三种模式，以及 run/expected step/parent、已消费 Gmail ID、logical hash、停止标记和精确 pending/active-worker owner。进程启动先记录 pending owner；Worker 精确认领并原子确认后才消费 Gmail ID、推进 expected step。Worker 在认领前死亡时，下一次 `poll-once` 清理 pending owner 并重试，步骤不会丢失。状态 JSON 原子替换，账本为追加 JSONL；inbox 暂存先写临时目录再原子发布。运行时数据默认在 `%LOCALAPPDATA%\AgentRelay\projects\gmail-courier`，不进入 Git。

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

`test-gmail` 只验证 OAuth 和连通性；`test-wake` 只验证 mock launcher。`run-agent` 是手动/外部启动 Relay Agent 的唯一 managed entry：它创建 pending owner，启动一个真实 Worker，Worker 负责读取 staged task、记录终端结果、写入 `handoff_obligations/<worker_id>.json`，并在固定 ChatGPT token 验证后才结束。直接打开的 naked Codex 属于 unmanaged debug process，不享受 AgentRelay 生命周期保证。每个 claimed Worker 都必须经历 `OPEN → RESULT_READY → SENDING → VERIFIED`；失败 handoff 不会删除债务，下一次 `poll-once` 会执行一次有界恢复。Watchdog 只能在 handoff `VERIFIED` 后启动。

`watchdog-ui` 是只读 Tkinter 监视器：它读取 `watchdogs/<RUN>-after-<STEP>.json`、状态快照和 JSONL 账本，显示启动确认、存活 PID、当前尝试、下一次 poll 倒计时、最近结果和终止原因。它不读取 Gmail、不启动 Worker，也不影响 watchdog；关闭窗口不会停止后台 watchdog。没有记录时会显示 `NO ACTIVE WATCHDOG`。

## 安全边界

新协议仍严格要求 `AGENTRELAY/1`、`CHANNEL`、`RUN`、`STEP`、`PARENT`、`DISPOSITION`、`PROJECT`。只接受配置的项目和稳定频道 `AR-GMAILCOURIER-A1R7P`。OAuth/token、inbox、state、logs 和 handoff 运行时目录均被忽略；日志不写入凭据。

本阶段删除了持久化 Runner、Supervisor 状态机、后台轮询线程、DRAINING/transport reconciliation、长寿命 App Server/醒门恢复层。保留的 Gmail gateway、协议解析、确定性暂存、哈希验证、原子状态和账本是唯一 Gmail 读取路径。ChatGPT 交接通过配置的固定 URL 一次性 sender 完成，不引入常驻服务。

`agent_relay.local_controller` is a separate certification-only local loop. Its Controller A owns a durable finite objective and examines Worker B's file result before making exactly one of `CONTINUE`, `COMPLETE`, or `HUMAN_REQUIRED`; B only executes a bounded file-backed task. It neither reads Gmail nor starts a watchdog or browser bridge. Each A→B and B→A handoff is held until the successor's exact ACK, claim (for B), and liveness record are durable.

## 验证

```powershell
python -m compileall -q agent_relay gmail_courier
python tools/certify_minimal_closure.py
python tools/certify_local_controller_loop.py
python tests/test_minimal_relay.py
python -m pytest tests/test_minimal_relay.py tests/test_courier.py
```

pytest 若使宿主崩溃，必须保留退出证据，不能把未完成运行报告为 PASS；`compileall` 和直接 unittest 是不依赖 pytest 的确定性检查。
