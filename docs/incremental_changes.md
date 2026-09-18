# 本次增量改动说明

> 本文总结本次基于 `docs/optimization.md` 和 `docs/devplan.md` 落地开发后的主要增量改动，重点说明功能层面的改进价值。

## 1. 总体概览

本次改动的目标，是把原有旅行助手从“单 Agent 调工具生成文本”的 Demo 型实现，升级为更接近生产系统的旅行规划 Agent：

- 主流程从隐式 prompt 驱动，升级为显式 LangGraph 工作流。
- 中间结果从自然语言为主，升级为结构化行程、预算、来源和审批数据。
- 外部工具从直接挂载，升级为 MCP 网关按能力筛选和降级加载。
- 对话输出从纯 token 流，升级为 token + 工具事件 + 结构化事件。
- 质量保障从人工体验为主，增加了 planner/eval 单元测试和基础评分器。

## 2. Agent 主流程改造

### 2.1 新增显式旅行规划图

新增 `app/agents/graphs/travel_planner_graph.py`，使用 `LangGraph StateGraph` 承载主流程。

当前主流程步骤：

```text
requirement_collection
  → destination_recommendation
  → transport_planning
  → accommodation_planning
  → food_planning
  → itinerary_generation
  → budget_summarization
  → order_generation
```

功能改进：

- 每轮对话根据 `current_step` 精确路由到当前节点。
- 每个节点只暴露当前阶段需要的工具。
- 节点执行前会检查前置字段，避免缺少目的地、交通、住宿等信息时继续编造。
- 后续支持步骤回退、局部重算和人工确认会更自然。

### 2.2 清理主 Agent 入口

重写 `app/agents/handoffs/travel_agent.py`，保留统一入口 `create_travel_agent()`，内部创建图式 Agent。

功能改进：

- 去掉旧的大工具集合单 Agent 入口，降低维护心智负担。
- 主流程统一走图式 Agent，避免同名函数覆盖导致行为不清晰。
- Postgres checkpoint 初始化失败时自动降级到 `MemorySaver`，本地开发更稳。

## 3. 状态与 Checkpoint 改进

### 3.1 扩展 TravelState

更新 `app/core/state.py`，新增：

- `structured_itinerary`
- `structured_budget`
- `source_references`
- `pending_approval`
- `approval_decision`
- `order_generation` 步骤枚举

功能改进：

- 前端不再只能解析自然语言，可以直接消费结构化行程和预算。
- 评测系统可以直接读取状态字段打分。
- 审批状态可以在 Agent 图中持久流转。

### 3.2 优化 Checkpointer

重写 `app/core/checkpointer.py` 的生命周期管理。

功能改进：

- 优先使用 `AsyncPostgresSaver` 支持会话恢复。
- 初始化失败会关闭连接池并抛出，避免半初始化状态。
- Windows 默认 `ProactorEventLoop` 下快速降级，避免 psycopg async pending task 残留。
- 主 Agent 创建时设置 3 秒超时，数据库不可用不阻塞本地调试。

## 4. 结构化规划能力

### 4.1 新增结构化 Schema

新增 `app/schemas/planning.py`，定义：

- `SourceReference`
- `MoneyRange`
- `LocationPoint`
- `PoiOption`
- `ItineraryItem`
- `ItineraryDayPlan`
- `ItineraryPlan`
- `BudgetItem`
- `BudgetEstimate`

功能改进：

- 行程、预算、地点、费用和来源都有稳定字段。
- 支持前端时间轴、地图、预算卡片展示。
- 支持 eval 读取字段做自动化评分。

### 4.2 新增行程规划器

新增 `app/planner/itinerary_planner.py`。

功能改进：

- 根据目的地、天数、旅行风格、亲子情况、餐饮偏好生成结构化行程。
- 每天包含上午、下午、晚上安排。
- 每个行程项包含耗时、费用区间、推荐理由、来源和交通提示。
- 带孩子时自动降低行程强度。
- 每天提供 Plan B，适配天气或疲劳等不确定情况。

### 4.3 新增预算估算器

新增 `app/planner/budget_estimator.py`。

功能改进：

- 替代简单硬编码预算。
- 按交通、住宿、餐饮、门票体验、市内交通和杂费拆分。
- 每项预算都有最小值、参考值、最大值。
- 预算超过用户上限时生成 warning，方便后续引导用户降本。

### 4.4 新增规划工具

新增 `app/tools/planning_tools.py`：

- `generate_itinerary_tool`
- `summarize_budget_tool`

功能改进：

- Agent 可以通过工具把结构化行程和预算写入 `TravelState`。
- 工具执行后自动推进 `current_step`。
- 保留自然语言摘要，兼容原有聊天体验。

## 5. MCP 工具体系升级

### 5.1 MCP Client 改造

重写 `app/mcp_core/client.py`。

功能改进：

- 支持按 server 加载 MCP 服务，避免一次性加载全部外部工具。
- 缺少环境变量时跳过对应 server，不影响其他工具。
- 单个 MCP server 加载失败时，其他 server 仍可用。
- 对第三方 MCP 工具缺失 `properties` 的 schema 做归一化，避免工具展示或测试崩溃。
- 根据请求 server 集合复用单例，减少重复启动本地 MCP 进程。

### 5.2 新增 MCP Gateway 和 Registry

新增：

- `app/mcp_core/gateway.py`
- `app/mcp_core/registry.py`

功能改进：

- 给 MCP 工具增加 capability 标签，例如 `weather.realtime`、`search.web`、`map.poi`、`hotel.search`、`transport.train`、`transport.flight`。
- 支持按 capability 筛选工具。
- 支持按 risk level 控制工具暴露范围。
- 为后续工具审计、超时、熔断和权限治理打基础。

### 5.3 MCP 工具选择器升级

更新 `app/tools/mcp_tools.py`。

功能改进：

- 酒店、天气、搜索、日期工具统一走 MCP Gateway 筛选。
- 规划阶段使用同一组 `PLANNING_MCP_SERVERS`，避免不同步骤反复重建 MCP client。
- MCP 不可用时返回空工具集，不中断主流程。

### 5.4 新增本地日期工具

更新 `app/mcp_core/servers/weather_server.py`，新增 `get_current_date`。

功能改进：

- 支持“今天、明天、后天、下周”等相对日期处理。
- 默认使用 `Asia/Shanghai` 时区。
- 解决需求收集阶段需要真实当前日期但没有日期工具的问题。

### 5.5 交通子 Agent 按需加载 MCP

更新：

- `app/agents/subagents/flight_agent.py`
- `app/agents/subagents/train_agent.py`
- `app/agents/subagents/driving_agent.py`
- `app/agents/subagents/transport_coordinator.py`

功能改进：

- 航班 Agent 只加载航班/搜索相关 server。
- 火车 Agent 只加载 12306/搜索相关 server。
- 自驾 Agent 只加载高德地图相关 server。
- MCP 异常时降级为空工具集，避免子 Agent 创建失败。

## 6. SSE 流式输出增强

更新 `app/api/v1/chat.py` 和新增 `app/schemas/streaming.py`。

新增事件类型：

- `token`
- `step_started`
- `step_completed`
- `tool_call`
- `tool_result`
- `itinerary_delta`
- `budget_update`
- `map_marker`
- `approval_required`
- `error`
- `done`

功能改进：

- 旧前端仍可消费 token 流。
- 新前端可以基于结构化事件实时渲染行程、预算、审批卡片。
- 工具调用开始和结束都有事件，方便调试和展示 Agent 工作过程。
- 当状态中出现 `structured_itinerary`、`structured_budget`、`pending_approval` 时，会分别输出结构化增量事件。

## 7. 人工审批机制

新增：

- `app/schemas/approval.py`
- `app/tools/approval_tools.py`

功能改进：

- 高风险动作可以先生成审批请求，而不是直接执行。
- 审批请求包含动作类型、风险级别、标题、摘要、工具名、工具输入、金额等信息。
- 用户确认或拒绝后，结果写入 `approval_decision`。
- 为后续接入真实下单、支付、发通知等动作提供安全边界。

## 8. RAG 来源与时效增强

更新：

- `app/rag/document_loader.py`
- `app/rag/pipeline.py`
- `app/tools/rag_tools.py`

功能改进：

- 文档 metadata 增加来源、城市、分类、更新时间、有效期等字段。
- RAG 检索结果可以返回 source 信息。
- 最终回答可以带证据来源，降低旅行信息过期或幻觉风险。
- 为后续 RAG eval 提供可评分字段。

## 9. 可观测与评测

### 9.1 Trace

新增 `app/observability/tracing.py`。

功能改进：

- 节点执行时可以记录轻量 trace。
- 方便排查某一步为什么失败或耗时异常。
- 为后续接入 LangSmith / dashboard 打基础。

### 9.2 Eval

新增：

- `evals/datasets/travel_tasks.jsonl`
- `evals/graders.py`
- `tests/test_evals/`
- `tests/test_planner/`

功能改进：

- 可以对旅行规划状态做自动评分。
- 当前评分维度包括需求完整性、行程可执行性、预算约束、来源覆盖率。
- 行程规划器和预算估算器有独立单元测试。

## 10. 文档增量

新增或更新：

- `docs/optimization.md`
- `docs/devplan.md`
- `docs/architecture.md`
- `docs/incremental_changes.md`

功能改进：

- `optimization.md` 说明为什么要优化、优化哪些方向、如何改。
- `devplan.md` 把优化拆成可执行开发计划。
- `architecture.md` 总结系统架构、技术栈和 Agent 设计。
- 本文档记录本次实际增量改动，方便 review 和面试讲解。

## 11. 验证结果

已通过：

```bash
uv run python -m compileall app evals tests\test_planner tests\test_evals
uv run pytest -q tests\test_planner tests\test_evals tests\test_mcp\test_weather_mcp.py tests\test_mcp\test_mcp_client.py::test_print_mcp_tools
```

结果：

```text
5 passed
```

主 Agent smoke test：

```text
create_travel_agent() 成功返回 CompiledStateGraph
```

全量测试：

```bash
uv run pytest -q
```

当前仍有失败，主要原因是外部 DashScope 服务返回：

```text
code: Arrearage
message: Access denied, please make sure your account is in good standing.
```

也就是模型或 embedding 服务账号欠费/不可用，属于外部依赖问题，不是本次新增 planner、MCP gateway、SSE、schema、审批等核心代码逻辑失败。

## 12. 功能改进总结

本次增量带来的核心功能提升如下：

| 改进方向 | 改动前 | 改动后 |
| --- | --- | --- |
| 主流程控制 | 单 Agent 依赖 prompt 自由推进 | LangGraph 显式步骤路由 |
| 状态管理 | 文本和零散字段为主 | `TravelState` 承载结构化规划状态 |
| 行程生成 | 偏模板化自然语言 | 结构化行程，含时间段、费用、原因、来源 |
| 预算估算 | 简单硬编码估算 | 分项区间预算，带 warning 和来源 |
| 工具接入 | 直接加载全部 MCP 工具 | 按 server/capability/risk 筛选 |
| 工具稳定性 | 单个外部服务失败可能影响整体 | MCP best-effort 加载和降级 |
| 前端输出 | 主要是 token 文本流 | token + 工具事件 + 行程/预算/审批事件 |
| 高风险动作 | 缺少明确确认机制 | 新增 approval request/decision |
| RAG 可信度 | 检索结果来源较弱 | 增强 source metadata 和 source 输出 |
| 质量保障 | 依赖手工体验 | 增加 planner/eval 测试和评分器 |

## 13. 后续改进
整体上，本次改动让项目从“能演示的 AI 旅行聊天助手”，向“可恢复、可评测、可扩展、可产品化的旅行规划 Agent 系统”推进了一大步。
- 前端地图、时间轴、预算面板直接消费 `structured_itinerary` 和 `structured_budget`。
- MCP gateway 增加真实重试、熔断、限流、统一结果 schema。
- RAG 增加独立 recall / MRR / citation coverage 评测。
- 多模态入口接入图片 OCR、攻略截图解析、语音转文本。
- 审批流程扩展到真实下单、支付、通知发送。
- Agent eval runner 批量执行 `travel_tasks.jsonl` 并输出报告。
