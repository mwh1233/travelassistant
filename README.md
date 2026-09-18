# TravelAssistant

TravelAssistant 是一个面向旅行规划场景的 AI Agent 系统。项目目标不是只做通用聊天问答，而是把用户的模糊旅行需求逐步转化为可执行、可修改、可恢复、可评测的结构化旅行方案。

系统当前支持多轮需求收集、目的地推荐、交通/住宿/餐饮规划、结构化行程生成、预算估算、RAG 检索、MCP 外部工具接入、SSE 流式输出、人工审批雏形和基础评测体系。

## 核心特性

- **显式 Agent 工作流**：使用 `LangGraph StateGraph` 管理旅行规划步骤，减少单 Agent 自由发挥导致的状态混乱。
- **结构化旅行结果**：行程、预算、地点、费用、来源等信息以 Pydantic schema 表达，方便前端渲染和自动评测。
- **MCP 工具网关**：天气、搜索、地图、酒店、火车、航班等能力通过 MCP 接入，并按 server、capability、risk level 筛选。
- **失败降级**：Postgres checkpoint、MCP server、第三方工具不可用时尽量降级，不让整个主流程直接崩溃。
- **RAG 证据增强**：本地攻略知识支持检索，并补充 source、updated_at、valid_until 等 metadata。
- **SSE 结构化事件**：除 token 流外，还支持工具事件、步骤事件、行程增量、预算更新和审批事件。
- **评测体系雏形**：提供 planner 单测、eval grader 和示例数据集，便于后续回归。

## 技术栈

| 类型 | 技术 |
| --- | --- |
| Web 服务 | `FastAPI`, `StreamingResponse` |
| Agent 编排 | `LangGraph`, `LangChain` |
| LLM 接入 | OpenAI-compatible `ChatOpenAI`, `ChatTongyi`, DashScope/Qwen |
| 数据库 | `PostgreSQL`, `SQLAlchemy AsyncSession` |
| 状态恢复 | `langgraph-checkpoint-postgres`, `MemorySaver` fallback |
| 缓存 | `Redis` |
| RAG | `Chroma`, DashScope Embeddings, BM25, jieba, reranker |
| MCP | `langchain-mcp-adapters`, `FastMCP` |
| Schema | `Pydantic` |
| 测试 | `pytest`, `pytest-asyncio` |
| 日志/观测 | `loguru`, LangSmith 配置预留 |

## 架构概览

```text
Client / Frontend
  │
  │ HTTP / SSE
  ▼
FastAPI API Layer
  ├─ /api/v1/chat/stream/{conversation_id}
  ├─ /api/v1/chat/history/{conversation_id}
  ├─ /api/v1/conversations
  └─ /api/v1/users
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
Tools Layer     MCP Layer        RAG Layer       Memory / Checkpoint
  │              │               │               │
  │              ├─ weather       ├─ Chroma       ├─ Postgres checkpoint
  │              ├─ search        ├─ Retriever    ├─ user memory
  │              ├─ amap          ├─ Reranker     └─ MemorySaver fallback
  │              ├─ 12306
  │              ├─ flight
  │              └─ hotel
  ▼
Structured Outputs
  ├─ itinerary_delta
  ├─ budget_update
  ├─ approval_required
  └─ final answer
```

更详细的架构说明见：`docs/architecture.md`。

## Agent 工作流

主 Agent 入口：`app/agents/handoffs/travel_agent.py`

主图定义：`app/agents/graphs/travel_planner_graph.py`

当前规划步骤：

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

执行方式：

1. 用户通过 SSE 接口发送消息。
2. 系统创建图式 Agent。
3. LangGraph 根据 `TravelState.current_step` 路由到当前节点。
4. 节点检查 `requires` 前置字段。
5. 节点加载当前阶段 prompt、用户记忆和工具。
6. 节点内部 Agent 执行。
7. 工具通过 `Command(update={...})` 更新 `TravelState`。
8. API 层将 token、工具事件、行程/预算/审批事件流式返回前端。

## TravelState

核心状态定义在：`app/core/state.py`

主要字段：

- `current_step`：当前旅行规划步骤。
- `user_requirement`：用户需求，包括出发地、目的地、日期、人数、预算、风格等。
- `selected_destination`：已确认目的地。
- `selected_transport`：已确认交通方式。
- `selected_accommodation_types`：住宿偏好或已选住宿类型。
- `selected_food_types`：餐饮偏好。
- `itinerary` / `structured_itinerary`：行程结果。
- `budget` / `structured_budget`：预算结果。
- `source_references`：来源和证据。
- `approval_pending` / `pending_approval` / `approval_decision`：高风险动作审批状态。

## MCP 工具机制

MCP 相关代码：

- `app/mcp_core/client.py`：MCP client 生命周期和工具加载。
- `app/mcp_core/gateway.py`：工具能力筛选和风险过滤。
- `app/mcp_core/registry.py`：工具 capability 注册。
- `app/tools/mcp_tools.py`：面向 Agent 阶段的 MCP 工具选择器。
- `app/mcp_core/servers/weather_server.py`：本地天气/日期 MCP server。
- `app/mcp_core/servers/search_server.py`：本地搜索 MCP server。

设计原则：

- **按 server 加载**：只连接当前需要的 MCP server。
- **按 capability 筛选**：例如 `weather.realtime`、`search.web`、`map.poi`、`hotel.search`。
- **按 risk level 控制**：当前默认只暴露 read 级工具，高风险动作走审批。
- **best-effort 降级**：部分 server 失败时，成功的工具继续可用。
- **schema 归一化**：修复部分第三方 MCP 工具缺少 `properties` 导致的兼容问题。

## 结构化规划

结构化 schema：`app/schemas/planning.py`

规划模块：

- `app/planner/itinerary_planner.py`
- `app/planner/budget_estimator.py`

Agent 工具：

- `app/tools/planning_tools.py`

能力说明：

- `generate_itinerary_tool`：根据当前状态生成结构化行程。
- `summarize_budget_tool`：根据当前状态生成结构化预算。

行程结果包含：

- 天数
- 每天主题
- 上午/下午/晚上安排
- 地点
- 耗时
- 费用区间
- 推荐理由
- 来源
- Plan B
- 强度等级

预算结果包含：

- 大交通
- 住宿
- 餐饮
- 门票/体验
- 市内交通和杂费
- 总预算区间
- 人均预算区间
- 估算说明和超预算 warning

## RAG

RAG 相关代码：

- `app/rag/document_loader.py`
- `app/rag/pipeline.py`
- `app/rag/retriever.py`
- `app/rag/reranker.py`
- `app/rag/vectorstore.py`
- `app/tools/rag_tools.py`

RAG 在本项目中的定位是“证据层”，主要用于补充目的地知识、攻略内容、注意事项和来源说明。

文档 metadata 支持：

- `source_url`
- `source_title`
- `city`
- `category`
- `updated_at`
- `valid_until`

## SSE 事件

流式接口：`POST /api/v1/chat/stream/{conversation_id}`

事件 schema：`app/schemas/streaming.py`

支持事件：

- `token`：模型输出 token。
- `step_started`：步骤开始。
- `step_completed`：步骤完成。
- `tool_call`：工具开始调用。
- `tool_result`：工具调用完成。
- `itinerary_delta`：结构化行程更新。
- `budget_update`：结构化预算更新。
- `map_marker`：地图点位事件预留。
- `approval_required`：需要用户确认。
- `error`：错误事件。
- `done`：流结束。

## 人工审批

审批相关代码：

- `app/schemas/approval.py`
- `app/tools/approval_tools.py`

用于写入、下单、支付等高风险动作：

```text
request_action_approval()
  ↓
TravelState.approval_pending = True
TravelState.pending_approval = ApprovalRequest
  ↓
SSE approval_required
  ↓
用户确认或拒绝
  ↓
record_action_approval()
```

当前审批是安全边界雏形，后续可接入真实订单、支付或通知系统。

## 项目目录

```text
app/
  agents/
    graphs/                  # LangGraph 主流程
    handoffs/                # 主 Agent 工厂与步骤配置
    routers/                 # 目的地等路由 Agent
    subagents/               # 航班/火车/自驾/交通协调子 Agent
  api/v1/                    # FastAPI 接口
  core/                      # 状态、checkpoint、store、middleware
  mcp_core/                  # MCP client、gateway、registry、本地 server
  planner/                   # 行程规划与预算估算
  rag/                       # RAG 加载、切分、检索、向量库
  schemas/                   # Pydantic schema
  tools/                     # LangChain tools
  observability/             # trace wrapper
docs/                        # 架构、优化、开发计划、增量说明
evals/                       # 评测数据和评分器
tests/                       # 单元测试和冒烟测试
```

## 环境变量

项目从 `.env` 加载配置。请不要提交真实密钥。

常用配置：

```env
APP_ENV=development
APP_HOST=0.0.0.0
APP_PORT=8000
DEBUG=false

DASHSCOPE_API_KEY=
QWEN_MODEL_NAME=qwen-max
QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1

POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=travelassistant
POSTGRES_USER=postgres
POSTGRES_PASSWORD=

REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=

AMAP_API_KEY=
TAVILY_API_KEY=
VARIFLIGHT_API_KEY=
AIGOHOTEL_MCP_API=

LANGSMITH_API_KEY=
LANGSMITH_PROJECT=travel-planner-dev
LANGSMITH_TRACING=true
```

说明：

- `DASHSCOPE_API_KEY` 用于 Qwen 模型和 embedding。
- `AMAP_API_KEY` 用于高德地图/天气。
- `TAVILY_API_KEY` 用于搜索 MCP。
- `VARIFLIGHT_API_KEY` 用于航班 MCP。
- `AIGOHOTEL_MCP_API` 用于酒店 MCP。
- Postgres 不可用时，主 Agent 会降级为内存 checkpoint。

## 安装

推荐使用 `uv`：

```bash
uv sync
```

如果使用 `pip`：

```bash
pip install -r requirements.txt
```

## 启动

Windows 推荐使用：

```bash
uv run python app/run.py
```

原因：`app/run.py` 会使用 `WindowsSelectorEventLoopPolicy`，避免 psycopg async 在 Windows 默认 `ProactorEventLoop` 下不兼容。

其他环境可使用：

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

服务启动后：

```text
GET /docs
GET /
```

## 主要 API

### 流式对话

```http
POST /api/v1/chat/stream/{conversation_id}
```

请求体使用 `MessageCreate`：

```json
{
  "content": "我想五一从北京出发，带孩子去西安玩 4 天，人均预算 3000"
}
```

返回为 SSE：

```text
data: {"type":"token", ...}
data: {"type":"tool_call", ...}
data: {"type":"itinerary_delta", ...}
data: {"type":"budget_update", ...}
data: {"type":"done", ...}
```

### 获取历史消息

```http
GET /api/v1/chat/history/{conversation_id}
```

## 测试

推荐先跑不依赖外部模型账号余额的定向测试：

```bash
uv run python -m compileall app evals tests\test_planner tests\test_evals
uv run pytest -q tests\test_planner tests\test_evals tests\test_mcp\test_weather_mcp.py tests\test_mcp\test_mcp_client.py::test_print_mcp_tools
```

当前已验证：

```text
5 passed
```

全量测试：

```bash
uv run pytest -q
```

注意：全量测试中部分用例会真实调用 DashScope/Qwen 或 DashScope Embeddings。如果账号欠费、Key 不可用或网络不可用，会出现类似：

```text
code: Arrearage
message: Access denied, please make sure your account is in good standing.
```

这类失败属于外部服务依赖问题，不代表本地 planner、MCP gateway、SSE schema 等核心逻辑失败。

## 文档索引

- `docs/architecture.md`：系统架构、技术栈、Agent 详细设计。
- `docs/optimization.md`：基于当前 AI 技术趋势的优化方案。
- `docs/devplan.md`：优化方案对应的开发计划。
- `docs/incremental_changes.md`：本次增量改动和功能改进说明。

## 当前状态

当前项目已经具备：

- 图式主 Agent。
- 结构化旅行状态。
- 结构化行程和预算生成。
- MCP 能力筛选和降级加载。
- RAG 来源增强。
- SSE 结构化事件。
- 人工审批雏形。
- 基础 eval 和 planner 测试。

后续可以继续增强：

- 真正的节点级 MCP 懒加载。
- MCP 调用结果统一内部 schema。
- 更完整的 RAG/Agent eval runner。
- 前端地图、时间轴、预算面板。
- 图片/语音多模态输入。
- 真实订单、支付和通知能力。
