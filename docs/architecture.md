# 旅行助手系统架构设计

> 本文用于说明当前旅行助手项目的整体架构、设计思路、技术栈，以及 Agent 工作流的详细设计。配套文档可参考 `docs/optimization.md` 和 `docs/devplan.md`。

## 1. 项目定位

本项目是一个面向旅行规划场景的 AI 助手系统，核心目标不是只做“聊天式问答”，而是把用户的模糊旅行需求逐步转化为可执行、可修改、可追踪的旅行方案。

系统需要支持：

- 多轮收集旅行需求，例如出发地、目的地、日期、天数、人数、预算、偏好、特殊限制。
- 根据需求推荐目的地，并解释推荐原因和风险。
- 查询或规划交通、住宿、餐饮、天气、搜索资料等外部信息。
- 生成结构化行程和预算，而不是只返回一段自然语言。
- 支持会话状态恢复、步骤回退、人工确认和后续扩展到订单/支付等高风险动作。
- 支持 RAG、MCP、评测、日志和 trace，便于持续迭代。

一句话概括：这是一个基于 LangGraph 的旅行规划 Agent 系统，外部能力通过 MCP 和 RAG 接入，最终输出结构化的旅行方案。

## 2. 设计思路

### 2.1 从“单 Agent 自由发挥”改成“显式工作流”

旅行规划天然是强流程任务。如果只让一个 Agent 挂载大量工具自由决定下一步，很容易出现：

- 需求没收集完整就开始规划。
- 工具调用顺序不稳定。
- 状态字段更新不一致。
- 用户回退或修改需求后，旧状态没有清干净。
- 最终行程和预算缺少可验证依据。

因此当前架构将主流程升级为显式 `StateGraph`：

```text
用户输入
  ↓
根据 current_step 路由到当前规划节点
  ↓
节点检查前置状态字段
  ↓
节点加载当前阶段 prompt 和工具
  ↓
节点内 Agent 执行
  ↓
工具通过 Command 更新 TravelState
  ↓
SSE 返回 token / 工具事件 / 结构化事件
```

这样做的核心收益是：每一步都有明确输入、输出、工具范围和状态更新边界。

### 2.2 中间结果结构化

项目不再只依赖自然语言文本传递信息，而是把关键结果结构化：

- 行程：`ItineraryPlan`、`ItineraryDayPlan`、`ItineraryItem`
- 预算：`BudgetEstimate`、`BudgetItem`、`MoneyRange`
- 来源：`SourceReference`
- 审批：`ApprovalRequest`
- SSE：`StreamEvent`

自然语言仍然用于用户沟通，但系统内部尽量使用结构化数据，方便：

- 前端渲染时间轴、地图点位、预算卡片。
- 后续重新计算和局部修改。
- 自动评测和回归测试。
- 和外部工具结果对齐。

### 2.3 工具按阶段暴露

不是所有工具都一次性挂给 Agent，而是按当前规划阶段暴露：

- 需求收集阶段：日期工具、记忆工具、需求记录工具。
- 目的地推荐阶段：目的地查询、搜索、偏好记忆。
- 交通阶段：交通查询和交通选择。
- 住宿阶段：酒店/地图相关 MCP 工具。
- 行程阶段：结构化行程生成工具。
- 预算阶段：结构化预算估算工具。
- 订单阶段：审批工具和订单生成工具。

这样可以降低误调用概率，也更利于排查问题。

### 2.4 外部能力统一走网关

天气、搜索、地图、酒店、航班、火车等外部服务通过 MCP 接入。MCP 服务数量增加后，直接把所有工具暴露给 Agent 会带来复杂度和稳定性问题，因此系统增加了 MCP 工具网关：

- 按 server 按需加载。
- 按 capability 过滤工具。
- 按 risk level 控制可用工具。
- 对第三方工具 schema 做归一化。
- 某个 MCP server 失败时不拖垮整个系统。

## 3. 技术栈

### 3.1 后端服务

- `FastAPI`：提供 HTTP API、SSE 流式接口。
- `SQLAlchemy AsyncSession`：数据库访问。
- `PostgreSQL`：业务数据、会话、消息、LangGraph checkpoint。
- `Redis`：缓存能力预留，RAG cache 使用。
- `Pydantic`：请求、响应、结构化规划、审批和 SSE schema。

### 3.2 Agent 与模型

- `LangChain`：创建节点内部 Agent、封装工具调用。
- `LangGraph`：构建显式旅行规划状态图。
- `ChatOpenAI` 兼容接口：当前配置对接 Qwen / DashScope 风格模型服务。
- `ChatTongyi`：部分旧路由和交通子 Agent 仍使用 Tongyi 调用。
- `LangGraph Checkpointer`：优先使用 `AsyncPostgresSaver`，不可用时降级 `MemorySaver`。

### 3.3 MCP 工具生态

- `langchain-mcp-adapters`：把 MCP tools 转为 LangChain tools。
- `FastMCP`：本地 weather/search MCP server。
- 外部 MCP server：
  - 高德地图：地图和 POI。
  - 12306 MCP：火车票/车次。
  - VariFlight：航班。
  - Aigohotel：酒店。

### 3.4 RAG

- `Chroma`：向量数据库。
- `DashScopeEmbeddings`：文本向量化。
- `BM25 / jieba`：关键词检索。
- 自定义 `HybridRetriever`、`Reranker`、`QueryOptimizer`、`Cache`：增强检索质量。
- 文档 metadata 扩展：`source_url`、`source_title`、`city`、`category`、`updated_at`、`valid_until`。

### 3.5 可观测与测试

- `loguru`：应用日志。
- `LangSmith`：trace 能力预留。
- `app/observability/tracing.py`：轻量 trace wrapper。
- `pytest` / `pytest-asyncio`：单元测试、MCP 冒烟测试、planner/eval 测试。
- `evals/graders.py`：结构化旅行状态评分器。

## 4. 总体架构

```text
Client / Frontend
  │
  │ HTTP / SSE
  ▼
FastAPI API Layer
  ├─ /chat/stream/{conversation_id}
  ├─ /history/{conversation_id}
  └─ users / conversations
  │
  ▼
Travel Agent Layer
  ├─ create_travel_agent()
  ├─ LangGraph StateGraph
  ├─ Step Agent Nodes
  └─ TravelState
  │
  ├──────────────┬───────────────┬───────────────┐
  ▼              ▼               ▼               ▼
Tools Layer     MCP Layer        RAG Layer       Memory/Checkpoint
  │              │               │               │
  │              ├─ weather       ├─ Chroma       ├─ Postgres checkpoint
  │              ├─ search        ├─ Retriever    ├─ user memory
  │              ├─ amap          ├─ Reranker     └─ fallback memory saver
  │              ├─ 12306         └─ Sources
  │              ├─ flight
  │              └─ hotel
  ▼
Structured Outputs
  ├─ itinerary_delta
  ├─ budget_update
  ├─ approval_required
  └─ final answer
```

## 5. 核心目录说明

```text
app/
  agents/
    graphs/                  # 显式 LangGraph 主流程
    handoffs/                # 主 Agent 工厂与阶段 prompt 配置
    routers/                 # 目的地等旧路由 Agent
    subagents/               # 航班/火车/自驾/交通协调子 Agent
  api/v1/                    # FastAPI 路由
  core/                      # TravelState、checkpoint、store、middleware
  mcp_core/                  # MCP client、gateway、registry、本地 server
  planner/                   # 确定性行程规划和预算估算
  rag/                       # 文档加载、切分、检索、向量库、缓存
  schemas/                   # Pydantic schema
  tools/                     # Agent tools
  observability/             # trace wrapper
evals/                       # 评测数据和 grader
tests/                       # 单元测试与冒烟测试
docs/                        # 架构、优化、开发计划文档
```

## 6. Agent 架构详解

### 6.1 主 Agent 入口

主入口在 `app/agents/handoffs/travel_agent.py`。

核心职责：

- 创建 LLM。
- 初始化 checkpointer。
- 创建 LangGraph 旅行规划图。
- 对 Postgres checkpoint 做超时和降级处理。

```text
create_travel_agent()
  ├─ get_llm()
  ├─ get_checkpointer()
  │   ├─ 成功：使用 Postgres checkpoint
  │   └─ 失败：降级 MemorySaver
  └─ create_travel_planner_graph()
```

这里保留 `MemorySaver` 作为降级方案，是为了保证本地开发、数据库不可用或 Windows 默认 ProactorEventLoop 下仍能启动主流程。

### 6.2 主工作流 StateGraph

主图定义在 `app/agents/graphs/travel_planner_graph.py`。

当前步骤序列：

```python
STEP_SEQUENCE = [
    "requirement_collection",
    "destination_recommendation",
    "transport_planning",
    "accommodation_planning",
    "food_planning",
    "itinerary_generation",
    "budget_summarization",
    "order_generation",
]
```

图的执行方式是“从 START 根据 `current_step` 路由到一个节点，节点执行后结束本轮”：

```text
START
  ↓ route_current_step(state.current_step)
对应节点
  ↓
END
```

这样设计是为了适配多轮对话：每次用户发来一轮消息，只执行当前步骤；如果工具更新了 `current_step`，下一轮自然进入后续步骤。

### 6.3 TravelState

`TravelState` 是整个 Agent 的状态载体，定义在 `app/core/state.py`。

它继承自 `AgentState`，保留 LangChain/LangGraph 的 `messages` 能力，同时扩展旅行规划字段：

- 流程控制：`current_step`
- 用户需求：`user_requirement`
- 用户选择：`selected_destination`、`selected_transport`、`selected_accommodation_types`、`selected_food_types`
- 候选结果：`destination_options`、`transport_options`、`accommodation_options`、`food_options`
- 最终结果：`itinerary`、`budget`、`report`
- 结构化结果：`structured_itinerary`、`structured_budget`、`source_references`
- 审批状态：`approval_pending`、`pending_approval`、`approval_decision`
- 元数据：`user_id`、`session_id`、`created_at`、`updated_at`

状态更新主要通过工具返回 `Command(update={...})` 完成，避免节点外部散乱修改状态。

### 6.4 节点执行模型

每个图节点由 `_build_step_node()` 生成，内部逻辑一致：

```text
节点开始
  ↓
检查 requires 前置字段
  ├─ 缺失：返回提示消息，不继续规划
  └─ 齐全：继续
  ↓
加载用户记忆
  ↓
用 Jinja2 渲染当前步骤 prompt
  ↓
create_agent(model, tools, state_schema, system_prompt)
  ↓
agent.ainvoke(state)
  ↓
返回 state update
```

这样每个阶段都可以有独立 prompt、独立工具集合和独立前置条件。

### 6.5 阶段配置

阶段 prompt 和工具配置在 `app/agents/handoffs/step_config.py`。

每个步骤配置包含：

- `prompt`：当前阶段系统提示词。
- `tools`：当前阶段可用工具。
- `requires`：执行该阶段前必须存在的状态字段。

示例结构：

```python
"transport_planning": {
    "prompt": "...",
    "tools": [
        select_transport_tool,
        go_back_to_destination,
        go_back_to_requirement,
        query_transport_options,
    ],
    "requires": ["user_requirement", "selected_destination"],
}
```

### 6.6 各 Agent 节点职责

| 节点 | 职责 | 主要输入 | 主要输出 |
| --- | --- | --- | --- |
| `requirement_collection` | 收集并确认用户需求 | 用户消息、记忆、日期工具 | `user_requirement` |
| `destination_recommendation` | 推荐或确认目的地 | 用户需求、搜索/目的地工具 | `selected_destination`、目的地候选 |
| `transport_planning` | 规划交通方式 | 需求、目的地 | `selected_transport`、交通候选 |
| `accommodation_planning` | 推荐住宿区域/酒店 | 需求、目的地、交通 | `selected_accommodation_types`、酒店候选 |
| `food_planning` | 确认餐饮偏好 | 需求、目的地、住宿 | `selected_food_types` |
| `itinerary_generation` | 生成结构化行程 | 已确认需求和选择 | `itinerary`、`structured_itinerary` |
| `budget_summarization` | 估算结构化预算 | 需求、行程、选择 | `budget`、`structured_budget` |
| `order_generation` | 订单/高风险动作入口 | 行程、预算 | 审批请求或订单信息 |

### 6.7 工具体系

Agent 使用的工具分为几类：

#### 状态推进工具

位置：`app/tools/state_transition.py`

用途：

- 记录需求。
- 选择目的地、交通、住宿、餐饮。
- 生成订单。
- 回退到指定步骤。

这些工具是流程状态变化的主要入口。

#### 结构化规划工具

位置：`app/tools/planning_tools.py`

包含：

- `generate_itinerary_tool`
- `summarize_budget_tool`

它们调用确定性规划模块：

- `app/planner/itinerary_planner.py`
- `app/planner/budget_estimator.py`

这部分的设计思想是：LLM 负责沟通、解释和偏好理解；行程结构和预算区间尽量由确定性逻辑生成，减少幻觉。

#### MCP 工具

位置：

- `app/tools/mcp_tools.py`
- `app/mcp_core/client.py`
- `app/mcp_core/gateway.py`
- `app/mcp_core/registry.py`

MCP 工具用于接入外部实时信息：

- 天气
- 搜索
- 地图/POI
- 酒店
- 火车
- 航班

当前 MCP 管理器具备：

- 按 server 请求工具。
- 缺少环境变量时跳过对应服务。
- 单个 server 加载失败时尽量保留其他工具。
- 对第三方 MCP 工具 schema 做兜底归一化。
- 通过 capability 和 risk level 做筛选。

#### RAG 工具

位置：

- `app/tools/rag_tools.py`
- `app/rag/pipeline.py`
- `app/rag/document_loader.py`

用途：

- 查询本地攻略、目的地知识、注意事项。
- 返回带来源 metadata 的检索结果。
- 为最终回答和评测提供证据链。

#### 记忆工具

位置：`app/tools/memory_tools.py`

用途：

- 更新用户旅行风格。
- 更新饮食禁忌。
- 更新口味偏好。
- 记录历史旅行目的地。
- 更新住宿偏好。

主图节点会在渲染 prompt 时尝试加载用户记忆，让 Agent 在多次会话中保持个性化。

#### 审批工具

位置：`app/tools/approval_tools.py`

包含：

- `request_action_approval`
- `record_action_approval`

用于高风险动作，例如写入、下单、支付。当前不会直接执行真实支付，而是把审批请求写入状态：

```text
approval_pending = True
pending_approval = ApprovalRequest
```

随后 SSE 会发送 `approval_required` 事件，前端可以展示确认卡片。

### 6.8 交通子 Agent

交通部分保留了多个子 Agent：

- `flight_agent.py`：航班查询。
- `train_agent.py`：高铁/火车查询。
- `driving_agent.py`：自驾路线。
- `transport_coordinator.py`：交通规划协调器。

子 Agent 的设计是为了把不同交通方式的 prompt、工具和参数要求隔离开。比如航班需要 IATA 三字码，火车需要车站编码，自驾需要地理编码和路径规划。

当前优化后，子 Agent 不再默认加载全部 MCP 工具，而是按需加载：

- 航班：`VariFlight-Aviation`、`search`
- 火车：`12306-mcp`、`search`
- 自驾：`amap`
- 协调器辅助工具：`weather`、`search`、`amap`

如果 MCP 不可用，子 Agent 会降级为空工具集，避免创建阶段直接崩溃。

## 7. 对话与 SSE 流式输出

SSE 入口在 `app/api/v1/chat.py`。

一轮流式对话过程：

```text
保存用户消息
  ↓
create_travel_agent()
  ↓
agent.astream_events(...)
  ↓
转换 LangGraph/LangChain events 为 SSE
  ↓
保存 assistant 文本消息
  ↓
发送 done
```

当前 SSE 事件类型包括：

- `token`：模型 token 增量。
- `step_started`：图节点开始。
- `step_completed`：图节点结束。
- `tool_call`：工具开始调用。
- `tool_result`：工具调用完成。
- `itinerary_delta`：结构化行程更新。
- `budget_update`：结构化预算更新。
- `approval_required`：需要用户确认。
- `error`：错误事件。
- `done`：结束事件。

这样的设计兼容旧聊天式前端，也支持新前端渲染卡片、地图、预算面板和审批卡片。

## 8. Checkpoint 与状态恢复

Checkpoint 管理在 `app/core/checkpointer.py`。

设计策略：

- 生产优先使用 `AsyncPostgresSaver`。
- 使用 `conversation_id` 作为 LangGraph `thread_id`。
- 本地或数据库不可用时降级为 `MemorySaver`。
- Windows 默认 `ProactorEventLoop` 不兼容 psycopg async 时快速失败并降级，避免残留 pending task。

恢复路径：

```text
conversation_id
  ↓
configurable.thread_id
  ↓
LangGraph checkpointer
  ↓
恢复上一轮 TravelState
```

## 9. RAG 架构

RAG 的定位是“证据层”，不是万能知识库。

当前链路：

```text
本地文档
  ↓
DocumentManager 加载并补充 metadata
  ↓
Text Splitter 切分 parent / child docs
  ↓
VectorStoreManager 写入 Chroma
  ↓
Retriever / Hybrid Retriever
  ↓
Reranker
  ↓
RAG tools 返回带 source 的上下文
```

RAG 输出会尽量携带：

- 来源标题
- 来源 URL
- 城市
- 分类
- 更新时间
- 有效期
- 置信度

这对旅行场景很重要，因为门票、开放时间、交通政策、酒店价格都具有时效性。

## 10. 结构化规划与预算

### 10.1 行程规划

`app/planner/itinerary_planner.py` 负责生成确定性结构化行程。

输入来自 `TravelState`：

- 目的地
- 出行天数
- 旅行风格
- 是否带孩子
- 餐饮偏好
- 住宿偏好

输出：

- 每天主题
- 上午/下午/晚上活动
- 地点
- 预计耗时
- 预计费用区间
- 推荐理由
- 交通提示
- Plan B
- 强度等级

### 10.2 预算估算

`app/planner/budget_estimator.py` 负责预算区间估算。

预算项包括：

- 大交通
- 住宿
- 餐饮
- 门票/体验
- 市内交通和杂费

每项预算都包含：

- `min_amount`
- `expected_amount`
- `max_amount`
- `currency`
- `estimated`
- `source`

如果估算超过用户预算上限，会给出 warning，方便 Agent 继续引导用户降本。

## 11. 评测体系

评测相关代码在：

- `evals/datasets/travel_tasks.jsonl`
- `evals/graders.py`
- `tests/test_evals/`
- `tests/test_planner/`

当前 grader 覆盖：

- 需求字段完整性。
- 行程可执行性。
- 预算约束符合度。
- 来源覆盖率。
- 综合得分。

评测设计重点是优先评结构化中间状态，而不是只看最终文本是否“看起来不错”。

## 12. 当前架构的扩展方向

后续可沿着以下方向继续增强：

- 前端地图、时间轴、预算面板直接消费 `structured_itinerary` 和 `structured_budget`。
- MCP gateway 增加真实重试、熔断、限流、统一结果 schema。
- RAG 增加独立 recall / MRR / citation coverage 评测。
- 多模态入口接入图片 OCR、攻略截图解析、语音转文本。
- 审批流程扩展到真实下单、支付、通知发送。
- Agent eval runner 批量执行 `travel_tasks.jsonl` 并输出报告。

## 13. 总结

当前架构的核心是：用 LangGraph 显式状态图控制旅行规划主流程，用 TravelState 承载可恢复状态，用阶段化 Agent 限定工具边界，用 MCP/RAG 提供外部证据和实时能力，用结构化 planner 生成可落地行程和预算，最后通过 SSE 输出文本和结构化增量事件。

这套设计让项目从“能聊天的旅行助手”升级为“可恢复、可验证、可扩展的旅行规划系统”。
