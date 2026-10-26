# ForgeFlow 2.0 全量实现契约（唯一执行依据）

> 项目定位：Verifiable & Self-Improving AI Coding Harness。状态：待实现规格，不是已完成声明。
> 目的：在现有仓库基础上（若空仓库则初始化）交付可运行、可恢复、可验证、可评测的 AI Coding Harness。所有模块必须具有真实代码、真实测试和证据，禁止只交文档、空壳接口或伪造基准。

## 0. 执行原则与优先级

1. 先审计当前仓库、Git 分支、未提交改动、已有架构、开发环境、权限、外部依赖和测试命令；先保存基线（commit/manifest），不删除、不覆盖用户已有成果。现有实现优先渐进迁移，避免无谓重写。若仓库为空，按本规范初始化。
2. 初次运行生成 `docs/forgeflow-v2/ARCHITECTURE.md`、`TASKS.md`（依赖 DAG、验收项）和 `STATUS.md`；随后按阶段 **设计 → 测试设计 → 实现 → 运行验证 → 复核 → 写入检查点** 连续推进，不停在“建议/草案”。
3. 工作面采用 `Control Plane / Execution Plane / Verification Plane / Evaluation Plane`；验证决策独立于 Agent 自述。只有可信执行器产生的绑定证据才能提升状态。
4. Phase 0–5 均是项目交付要求，不能以“后续建议”绕过。API 密钥、真实 GPU/FPGA、外部账号等缺失时必须实现可测试的适配器/契约测试，将外部集成明确报告 BLOCKED；不得声称真实联调成功。
5. 优先采用官方稳定协议；升级兼容采用 Adapter、可替换后端，锁定依赖范围并提供版本记录。禁止照搬其他仓库代码导致许可证问题；注明来源。
6. 每阶段产生可运行垂直切片并保存 `evidence/`（命令、退出码、时间、Git SHA、环境摘要、测试报告、制品 SHA-256）；每阶段至少 1 个真实端到端场景。不得修改测试期望来遮掩实现缺陷。
7. 安全：禁止提交密钥、绕过权限、静默运行危险 shell、禁用安全门禁、默认开放公网端口、自动推送远程仓库或部署到生产。隔离未知代码，命令白名单/审批、超时、资源限制、脱敏日志。无人工批准不执行不可逆外部副作用。
8. 设计/实现评审可使用独立 SubAgent，但实际状态、测试和交付由确定性程序判定；默认单 Agent，只有任务可并行且工作区隔离才并行。模型调用失败需限次退避并保留证据。
9. Windows + WSL2 为优先使用环境，Linux CI 必须可运行。需要 Docker 的功能应检测 Docker 可用性，不可用时报 BLOCKED，并提供无 Docker 的 Mock 验证路径。
10. 全程尽量自行排查并推进，只有破坏性操作、真实费用、权限授权、外部硬件等必要场景才请求用户确认。不得因为局部阻塞而停止全部独立任务。

## 1. 交付范围：技术栈与可执行项目结构

- Python >=3.11，建议 `uv`、Typer、Rich、Pydantic v2、SQLite（WAL）、pytest、Hypothesis、Git CLI、Tree-sitter、OpenTelemetry；必要时 pyright/ruff。
- 默认本地 CLI-first，无需复杂 UI；尽量做到纯 Python 核心 + 可选 extras。
- 推荐目录（若仓库存在成熟结构，可保持等价分层）：
  - `src/forgeflow/{cli,domain,planner,state,events,runner,agents,context,verification,recovery,observability,evaluation,adapters,delivery}/`
  - `tests/{unit,integration,e2e,chaos,security,fixtures}/`
  - `examples/{basic_goal,crash_resume,clean_room,pi_rpc,context_budget,parallel_tasks,fpga_adapter}/`
  - `docs/{architecture,contracts,protocols,benchmarks,runbooks,forgeflow-v2}/`
  - `.github/workflows/ci.yml`、`pyproject.toml`、`README.md`、`LICENSE`（如原仓库已有则保留）
- 核心 CLI 至少包括：`forge init`、`goal`、`plan`、`run`、`quick`、`status`、`verify`、`resume`、`recover`、`context`、`benchmark`、`deliver`、`doctor`。
- 提供 `forge --help`、`forge doctor --json`、机器可解析 JSON 状态输出；至少一个 installable 包和从零开始跑通的演示脚本。

## 2. Phase 0：基线审计与架构落地

**产出：** 仓库审计、现状模块复用清单、架构 ADR、任务 DAG、风险表、验收矩阵、版本/许可证核对、简短开发指南。

- 核实官方 docs：Claude Code 原生 `/goal`、Pi RPC、Tree-sitter、ACP v1、MCP 2026-07-28、OpenTelemetry GenAI、Harbor；不要把 ACP 当 Agent-to-Agent 协议，也不要把 MCP 当调度器。
- 设计领域对象：Goal、GoalRevision、Task、TaskAttempt、Dependency、Policy、AgentRun、Event、Evidence、Gate、Artifact、Checkpoint、BenchmarkRun、Decision。
- 为这些对象定义 Pydantic Schema、JSON 序列化、数据库迁移和 ID 命名方式；写状态转换表以及非法转换处理策略。
- 定义领域事件（如 GoalCreated、TaskClaimed、AttemptStarted、ArtifactBuilt、EvidenceRecorded、GateEvaluated、TaskBlocked、TaskVerified），每一事件包含 task_id、goal_id、timestamp、sequence、correlation_id、revision。
- 将本文件每个 Phase 的验收项转换成机器可检查或人工签名的明确测试；明确哪些需要真实外部环境才能证明。

## 3. Phase 1：Durable Goal Runtime（可信执行内核）

**必备：**
1. Goal Compiler：从自然语言/结构化 YAML 定义生成 Goal Spec；无模型时支持 YAML deterministic path；保存目标、约束、验收标准与输入版本。自然语言解析可经 Adapter，禁止假装模型可用。
2. Task Graph：DAG 检测、硬/软依赖、ready frontier、优先级、atomic claim、owner、lease/fencing token、取消、暂停、恢复、最大重试数，避免双重认领和僵尸 worker。
3. 持久化：SQLite WAL、显式事务、结构化审计事件、可重放状态投影；进程在状态写入前后被杀死，不应静默丢失成功或重复执行有副作用的任务。
4. CommandRunner：受控 cwd、环境变量 allowlist、timeout、stdout/stderr 截断与完整落盘、exit code、PID/process-group 清理；阻止路径逃逸；涉及写入/网络等命令按策略授权。
5. Checkpoint / Resume：重启后识别 `UNKNOWN / INTERRUPTED / BLOCKED` 等状态；外部副作用使用显式幂等键或人工处理，切勿承诺跨进程 exactly-once 执行。
6. Risk Quick Mode：分文档/一般代码/关键模块/外部副作用等风险等级；快速模式能缩短计划，但不得跳过必要的风险检查、证据验收与回滚策略。
7. 把模拟 Agent（确定性实现）跑通从 Goal 到交付的端到端闭环；每步产出真实事件和数据库记录。

**硬性测试：** 循环依赖拒绝、未通过依赖不释放下游、并发原子认领、过期 lease 防复写、进程硬杀/重启恢复、重复事件幂等处理、不可逆动作不自动重放、权限拒绝、命令超时、事件重放状态一致。

## 4. Phase 2：Evidence Provenance Graph + Clean-Room Verifier（主要差异化）

- 证据不得仅是 Agent 生成的文字。结构化 Evidence 至少绑定 goal_revision、task_attempt、repo commit/tree/diff fingerprint、environment digest、command、exit code、report digest、runner identity、timestamp、scope、verifier policy version。
- 证据图边：`Goal -> Contract -> Task -> Attempt -> CodeSnapshot -> TestRun -> Evidence -> Gate -> Artifact`；提供查询 API 能回答“哪个版本的哪项测试证明当前交付满足哪条契约”。
- 增量失效：源码/测试/锁文件/工具版本/相关依赖变化后，按 scope 失效或要求重验；保守回退策略，不能无根据沿用旧 PASS。
- Gate Engine：`PENDING/PASS/FAIL/BLOCKED/STALE`；静态类型、lint、unit、integration、regression、contract、build、security、artifact、manual/hardware 是不同 gate；无证据不允许 PASS。
- Clean Room：隔离临时工作区从可信 commit 应用补丁、重装锁定依赖、运行不可由 Agent 修改的 verifier 配置；可选 Docker 沙箱并有限权/无网（不适用者记录原因）；严禁直接信任 agent 修改过的测试环境或用 Agent 文本捏造。
- 供应链：manifest 包含 SHA-256 和 Git SHA，支持导出 JSON/Markdown；哈希保证完整性而非身份真实性；若未来增加签名需独立的密钥信任根。
- 专项安全用例：Agent 宣称 PASS 但命令失败、伪造 stdout、测试脚本被改成永远通过、跨源码版本重用证据、环境摘要错配、旧文件/脏树复验、缺少人工验收。

**硬性演示：** 相同任务先显示“自述成功，但因证据不足判 BLOCKED”，补齐可信测试后变 PASS；随后修改源码，旧证据立刻 STALE；独立干净工作区复验可运行。

## 5. Phase 3：Context Engineering + Agent Interoperability

### 5A. 结构感知 Context Engine
- Tree-sitter 解析 Python 与至少一种其他语言（建议 TS/JS）；提取模块、符号、定义、签名、行范围；针对不支持语言提供清晰的 text fallback。LSP 与复杂跨模块调用图可按可用性实现插件，不许误称 Tree-sitter 自带完整语义依赖图。
- Git Diff 识别改动符号，结合显式 import/call/reference、相关测试、需求契约与历史失败记录形成可解释检索分数。设计权重配置和 token budget 截断，保留强制契约文件。
- 存储索引缓存、增量更新、失效机制、检索解释（选入原因、来源行号、近似 token 数、未纳入原因）。避免任意项目数据外泄。
- 与全文件加载、简单关键词检索、Diff-only 建立实验对照；不是只比 Token，必须比同预算下实际正确完成率。

### 5B. Agent Adapter
- 标准 `AgentAdapter`：start/submit/cancel/events/wait_until_settled/status/cost/trace；区分命令被接受和 Agent 实际完成。
- `MockAdapter` 可脱离外部服务完全通过 E2E；`PiRpcAdapter` 优先接入 `pi --mode rpc` 的 stdin/stdout JSONL，处理 request ID、流事件、`agent_settled`、崩溃和关闭；有本地 Pi 时进行真实 smoke test，否则标示 EXTERNAL_BLOCKED。
- 再提供一种真实 Agent 接入（优先 Claude Code CLI 非交互方式，有权限和登录时真实运行），保留 credential-independent 合约测试和错误分类；不得要求用户将密钥写入代码。
- MCP：实现兼容 `2026-07-28` 的可选工具适配器，遵循无状态核心/任务扩展能力协商；无协议特性时优雅降级。
- ACP：可选基于 v1 的 IDE 连接/代理入口，至少有功能边界文档和带 mock 的协议合约测试；不能把 ACP v2 草案当稳定协议强依赖。

### 5C. 并行多 Agent 调度
- 有限并发、按依赖/文件 ownership 选择 worker、隔离 Git Worktree、统一变更收敛/冲突报告、合并后强制回归。不要默认同时打开 5 个 LLM Worker。
- 验证冲突时可重试/串行回退；孤儿 worktree 可回收，但绝不删除非本框架拥有的工作树。

## 6. Phase 4：Recovery + Observability + Evaluation

### Recovery Intelligence
- 将失败分类：compile/type/lint/test/dependency/environment/network/timeout/resource/permission/contract/external-blocker；分类支持规则基线与可插拔 LLM Debugger。
- 最小修复 diff、限次尝试、同错熔断、记录尝试成本、自动复验；修复后必须跑失败相关测试+受影响回归，不允许通过删除测试解决。
- 外部状态不可用时切换 BLOCKED，并且继续执行不依赖该条件的任务。

### Observability
- 标准结构化 Trace/Span：goal -> task -> agent -> tool -> verifier；采集耗时、尝试数、异常、使用量（仅可获得时）；OpenTelemetry 支持本地无 collector fallback。
- 默认不记录 API key、完整原始 Prompt、私有代码片段；实现 scrubber、可配置的保留期限和敏感数据脱敏测试。

### Evaluation and Optimization
- `forge benchmark`：支持自建可复现任务集，并设计 Harbor bridge（实际 Harbor 环境可用时至少运行一轮；不可用时运行内部 fixture benchmark，记录 external blocked）。
- 四组：A 原生 Agent；B OpenSpec/GSD；C ForgeFlow Core；D ForgeFlow Full。若基线工具/凭据不足，标为 NOT_RUN，不允许虚构比较数据。
- 指标：verified success rate、false completion rate、recovery rate、cost per verified task、token、latency、regression rate、merge conflict rate；同 task/model/budget/environment 下多轮，保存原始 JSONL/CSV、汇总表和失败案例。
- 至少做禁用 Context、禁用 Recovery、禁用 Reviewer/并行的消融。实现 `PolicyCandidate`、离线验证、阈值审批和版本化 rollout；改进失败必须能 rollback，不能对未测任务自动声称优化有效。

## 7. Phase 5：领域适配、交付和发布

- Python/GitHub PR adapter：pytest、ruff、typecheck、覆盖率与 CI 结果分开报告；未连接 GitHub 时用本地模拟 CI + 契约测试。
- FPGA adapter：Icarus RTL 仿真、构建/资源/时序报告解析接口、bitstream SHA-256、上板人工/硬件证据状态；没有实物绝不可宣称硬件通过，示例用 fixture。
- 通用 Agent app adapter：任务轨迹、检索、调用结果和风险门禁；至少一条 demo。
- 生成 `forge deliver`：版本、功能范围、测试命令及退出码、依赖锁文件、各 gate 状态、未验收事项、manifest 与可重放步骤；`READY` 必须是策略所需门禁全部通过。
- README（定位、差异化、安装、架构、CLI 示例、实测结果、限制）、CONTRIBUTING、CHANGELOG、许可与引用、CI；Windows PowerShell + WSL2 + Linux 指南；如自研运行时需说明为何不直接使用 Beads/Temporal。
- 最终做安全审查、独立代码审查、完整单元/集成/E2E/故障注入测试，以及至少 1 条 **有真实工具输出** 的端到端演示。

## 8. 统一验收门禁（不可跳过）

| Gate | 完成标准 |
|---|---|
| G0 | 仓库审计、ADR、风险/任务 DAG、兼容性矩阵真实存在且与代码一致 |
| G1 | 可安装，CLI 子命令帮助正常，Windows/WSL 操作步骤可复现 |
| G2 | Goal/Task/State/Event/Resume 功能与故障注入全部通过 |
| G3 | 独立证据、版本绑定、干净环境复验、污染拦截、失效机制通过 |
| G4 | AST/增量检索、可解释排序和预算裁剪通过真实代码 fixture 测试 |
| G5 | Mock + Pi RPC + 第二 Agent 适配器合约完整；真实连通性缺失须 BLOCKED |
| G6 | 并行隔离、冲突处理、合并后回归与有界自修复通过 |
| G7 | OTel trace 脱敏和评测记录/对照/消融功能可复现；未跑基线报告 NOT_RUN |
| G8 | Python/FPGA/Agent 三类适配器都有可运行的合约或 fixture 演示，外部硬件不造假 |
| G9 | 全部自动化测试、CI、文档、交付 manifest 与未解决阻塞清单一致 |

- 验收禁止事项：仅提供 Mock 却报告真实联调；只生成 spec/demo 不落代码；跳过失败测试；替换成无关 stub；将缺少凭据或真实设备伪造为 PASS；把“任务执行指令已发出”视为任务完成；不重验合并后的代码。
- 为每个 Gate 输出 `PASS/FAIL/BLOCKED/NOT_RUN`、依据、对应命令、Git SHA、可见输出、证据路径。`BLOCKED`、`NOT_RUN` 不能计入通过率。
- 对接未完成但项目核心已运行时，只能报告“core verified / integrations blocked”，不得报告 100% 完成。

## 9. Checkpoint 和最终答复格式

每阶段完成必须更新：
- `docs/forgeflow-v2/STATUS.md`：当前 Phase、Gate 表、可恢复下一步、已知问题、环境阻塞、证据路径；
- `docs/forgeflow-v2/TASKS.md`：任务状态、依赖、变更 SHA、结果；
- `evidence/manifest.json`：测试/构建/环境/哈希的索引；
- `docs/forgeflow-v2/DECISIONS.md`：技术选型的依据、替代方案、可回滚策略。

最终向用户报告：
1. 实际完成了哪些模块与可直接运行的命令；
2. 全部 Gate 的 PASS/FAIL/BLOCKED/NOT_RUN 状态；
3. 真实测试/构建输出和 SHA；
4. 基准实验数据与对照结果（若没有必须明确说没有）；
5. 外部服务/设备阻塞及最小人工操作；
6. 未完成事项，不允许主观“全部完成”。

## 10. 参考文档（实现前核对最新版）

- Claude Code Goal: https://code.claude.com/docs/en/goal
- OpenSpec: https://github.com/Fission-AI/OpenSpec
- Spec Kit: https://github.com/github/spec-kit
- Beads: https://github.com/gastownhall/beads
- Gas Town: https://github.com/gastownhall/gastown
- Pi / RPC: https://github.com/earendil-works/pi/tree/main/packages/coding-agent/docs
- ACP v1: https://agentclientprotocol.com/protocol/v1/overview
- MCP 2026-07-28: https://modelcontextprotocol.io/specification/2026-07-28
- Tree-sitter: https://tree-sitter.github.io/tree-sitter/
- Harbor: https://github.com/harbor-framework/harbor
- OpenTelemetry GenAI: https://opentelemetry.io/docs/specs/semconv/gen-ai/
- Temporal: https://docs.temporal.io/
