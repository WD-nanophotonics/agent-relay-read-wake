# AgentRelay — Minimal Two-Shot Relay

本仓库保留 `gmail-courier` 作为 Gmail/OAuth 参考实现，并提供一个短命、可审计的 `agent-relay`。生产路径不是常驻 Supervisor：

```text
Python Gmail poll-once
  └─ stage at most one → spawn exactly one Worker
       └─ bounded work / checks / commit / push / fixed ChatGPT handoff
            └─ detached watchdog (最多两次 poll-once) → Worker exits
```

“AI 永不等待，软件等待”。`poll-once` 只执行一个 Gmail fetch cycle，不 sleep、不循环、不通过 Codex Gmail integration 读取邮件。已有活动的精确 Worker owner 会使本次调用无操作；旧步骤忽略，未来有效步骤暂缓且不消费，同一 logical step 的不同哈希 fail closed。`NO_ACTION` 只推进协议序号，`WAKE` 才暂存并启动 Worker，`HUMAN_REQUIRED` 不启动 Worker。

持久化只有 `IDLE`、`BUSY`、`STOPPED` 三种模式，以及 run/expected step/parent、已消费 Gmail ID、logical hash、停止标记和精确 active-worker owner。状态 JSON 原子替换，账本为追加 JSONL；inbox 暂存先写临时目录再原子发布。运行时数据默认在 `%LOCALAPPDATA%\AgentRelay\projects\gmail-courier`，不进入 Git。

## 命令

```powershell
python -m pip install -e ".[dev]"
agent-relay init
agent-relay poll-once
agent-relay status
agent-relay stop
```

`test-gmail` 只验证 OAuth 和连通性；`test-wake` 只验证 mock launcher。Worker 使用配置的 Codex 命令执行单个暂存任务，完成后写固定格式 handoff 报告并启动一次 detached watchdog。Watchdog 用 `project + RUN + AFTER_STEP` 精确锁去重，最多等待/调用两次 `poll-once`，绝不打断活动 Worker、递归唤醒或常驻运行。

## 安全边界

新协议仍严格要求 `AGENTRELAY/1`、`CHANNEL`、`RUN`、`STEP`、`PARENT`、`DISPOSITION`、`PROJECT`。只接受配置的项目和稳定频道 `AR-GMAILCOURIER-A1R7P`。OAuth/token、inbox、state、logs 和 handoff 运行时目录均被忽略；日志不写入凭据。

本阶段删除了持久化 Runner、Supervisor 状态机、后台轮询线程、DRAINING/transport reconciliation、长寿命 App Server/醒门恢复层。保留的 Gmail gateway、协议解析、确定性暂存、哈希验证、原子状态和账本是唯一 Gmail 读取路径。真实 Codex thread/wake、Chrome/ChatGPT 交接的扩展点仍由固定 handoff 报告承载，不在本阶段引入常驻服务。

## 验证

```powershell
python -m compileall -q agent_relay gmail_courier
python tests/test_minimal_relay.py
python -m pytest tests/test_minimal_relay.py tests/test_courier.py
```

pytest 若使宿主崩溃，必须保留退出证据，不能把未完成运行报告为 PASS；`compileall` 和直接 unittest 是不依赖 pytest 的确定性检查。
