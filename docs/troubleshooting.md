# TravelAssistant 排障思路

本文档用于在项目出现异常时，快速判断问题发生在哪个环节，并给出可执行的定位步骤。

## 1. 总体原则

排障时不要直接猜是模型、工具或数据库的问题，先按请求链路逐段缩小范围。

主链路如下：

```text
HTTP/SSE 请求
  -> 鉴权与会话校验
  -> 保存用户消息
  -> 创建 Travel Agent
  -> LangGraph 当前步骤路由
  -> LLM 推理与工具调用
  -> 保存 AI 回复
  -> SSE done
```

核心判断依据：

- 如果接口没有进入业务逻辑，优先查启动、路由、鉴权、数据库连接。
- 如果接口进入了但没有流式事件，优先查 Agent 创建、checkpointer、MCP 初始化、模型配置。
- 如果有步骤事件但流程卡住，优先查 LangGraph 状态字段和步骤依赖。
- 如果有工具调用但无结果，优先查具体工具、MCP、RAG 或外部 API。
- 如果模型正常输出但历史记录缺失，优先查消息保存和数据库事务。

## 2. 关键文件地图

| 环节 | 文件 | 关注点 |
| --- | --- | --- |
| 应用启动 | `app/main.py` | FastAPI lifespan、checkpointer、store、MCP 初始化 |
| API 入口 | `app/api/v1/chat.py` | SSE 流、会话校验、消息保存、错误返回 |
| Agent 创建 | `app/agents/handoffs/travel_agent.py` | LLM 配置、checkpointer fallback、graph 创建 |
| Graph 编排 | `app/agents/graphs/travel_planner_graph.py` | 当前步骤路由、步骤依赖、prompt 渲染、trace |
| 步骤配置 | `app/agents/handoffs/step_config.py` | 每个步骤的 prompt、tools、requires |
| 状态定义 | `app/core/state.py` | `current_step` 和各阶段状态字段 |
| 状态流转工具 | `app/tools/state_transition.py` | 记录需求、选择目的地、交通、住宿、餐饮、生成行程和预算 |
| MCP 客户端 | `app/mcp_core/client.py` | MCP server 配置、环境变量、工具加载 |
| MCP 工具选择 | `app/tools/mcp_tools.py` | 按能力筛选 hotel/search/weather/date 工具 |
| RAG 工具 | `app/tools/rag_tools.py` | 文档加载、向量库加载、检索异常 |
| 数据库 | `app/models/base.py` | async engine、session、事务提交/回滚 |
| 日志 | `app/utils/logger.py`、`app/observability/tracing.py` | `logs/app.log`、`logs/error.log`、trace 事件 |

## 3. 第一现场：先看 SSE 事件

聊天接口会输出统一的 SSE 事件。排障时先判断最后一个成功出现的事件类型。

| 现象 | 初步判断 |
| --- | --- |
| 请求直接 401/403 | 鉴权失败，查 `get_current_user` 和 token |
| 请求直接 404 | 会话不存在或不属于当前用户 |
| 只保存了用户消息，无后续事件 | Agent 创建或 graph 初始化失败 |
| 出现 `step_started` 后报错 | 当前步骤执行失败，查 state、prompt、LLM、工具 |
| 出现 `tool_call` 后卡住 | 工具调用失败或超时，查 MCP/RAG/外部 API |
| 有 `token` 但没有 `done` | 流式生成中断，查异常日志 |
| 有 `done` 但历史记录没有 AI 回复 | assistant 消息保存失败或内容为空 |

SSE 事件生成位置：

- `stream_event()`：`app/api/v1/chat.py`
- `generate_sse_stream()`：`app/api/v1/chat.py`

## 4. 日志排查

实时查看日志：

```powershell
Get-Content logs\app.log -Wait -Tail 100
Get-Content logs\error.log -Wait -Tail 100
```

快速搜索关键错误：

```powershell
rg "ERROR|Exception|trace_error|MCP|RAG|checkpointer|Travel Agent" logs app
```

重点日志关键词：

- `trace_start name=travel_planner_step`：某个 LangGraph 步骤开始执行。
- `trace_end name=travel_planner_step`：某个步骤正常结束。
- `trace_error name=travel_planner_step`：步骤内部异常。
- `Creating graph-based Travel Agent`：开始创建 Agent。
- `Postgres checkpointer unavailable`：Postgres checkpointer 不可用，已降级到内存。
- `Initializing MCP servers`：开始初始化 MCP。
- `Skipping MCP server`：某个 MCP server 因环境变量缺失被跳过。
- `MCP server ... failed to load tools`：某个 MCP server 工具加载失败。
- `RAG search failed`：RAG 检索失败。

注意：当前仓库中部分中文注释或日志在某些终端里可能显示乱码。排障时优先依赖英文函数名、异常栈、结构化字段和 trace 关键字。

## 5. 分层定位流程

### 5.1 服务启动失败

检查项：

- Python 版本是否满足 `>=3.11`。
- 依赖是否安装完整。
- `.env` 是否存在必要配置。
- Postgres、Redis 是否可连接。
- Windows 下事件循环是否通过 `app/run.py` 启动。

常用命令：

```powershell
python --version
python -m pytest tests/test_api -q
python app/run.py
```

重点文件：

- `app/run.py`
- `app/main.py`
- `app/config.py`
- `app/models/base.py`

### 5.2 API 请求失败

检查项：

- 路由是否正确：`/api/v1/chat/stream/{conversation_id}`。
- 用户 token 是否有效。
- conversation 是否存在。
- conversation 是否属于当前用户。
- 数据库 session 是否正常提交或回滚。

重点文件：

- `app/api/v1/chat.py`
- `app/api/dependencies.py`
- `app/models/conversation.py`
- `app/models/message.py`

定位方式：

1. 看 HTTP 状态码。
2. 看是否进入 `stream_chat()`。
3. 看 `Conversation` 查询是否返回结果。
4. 看 `save_message()` 是否成功写入用户消息。

### 5.3 Agent 创建失败

检查项：

- `DASHSCOPE_API_KEY` 是否配置。
- `QWEN_MODEL_NAME`、`QWEN_BASE_URL` 是否正确。
- Postgres checkpointer 是否可用。
- MCP 初始化是否拖慢或失败。
- `create_travel_planner_graph()` 是否成功 compile。

重点文件：

- `app/agents/handoffs/travel_agent.py`
- `app/agents/graphs/travel_planner_graph.py`
- `app/core/checkpointer.py`
- `app/mcp_core/client.py`

定位方式：

1. 搜索 `Creating graph-based Travel Agent`。
2. 如果之后没有 `Travel Agent graph created`，说明创建阶段失败。
3. 如果出现 `Postgres checkpointer unavailable`，说明 checkpointer 有问题但系统会降级为内存。
4. 如果 MCP 相关日志大量超时，先临时确认是否外部工具初始化导致阻塞。

### 5.4 LangGraph 步骤走错或卡住

检查项：

- `current_step` 是否在合法步骤列表内。
- 当前步骤的 `requires` 字段是否都已存在。
- 上一步工具是否正确更新 state。
- prompt 模板渲染是否因字段缺失失败。

步骤顺序：

```text
requirement_collection
destination_recommendation
transport_planning
accommodation_planning
food_planning
itinerary_generation
budget_summarization
order_generation
```

关键状态字段：

- `current_step`
- `user_requirement`
- `selected_destination`
- `selected_transport`
- `selected_accommodation_types`
- `selected_food_types`
- `itinerary`
- `budget`

重点文件：

- `app/core/state.py`
- `app/agents/graphs/travel_planner_graph.py`
- `app/agents/handoffs/step_config.py`
- `app/tools/state_transition.py`

定位方式：

1. 从日志中确认当前执行的 step。
2. 对照 `step_config.py` 里的 `requires`。
3. 如果缺字段，回查上一个状态流转工具是否被调用。
4. 如果 step 异常，查 `trace_error` 对应的异常栈。

### 5.5 LLM 没有输出或输出异常

检查项：

- API key 是否为空或无效。
- 模型名是否支持当前接口。
- base URL 是否正确。
- token 上限是否过低。
- prompt 是否包含无效变量或过长上下文。

重点文件：

- `app/agents/handoffs/travel_agent.py`
- `app/agents/handoffs/step_config.py`
- `app/config.py`

定位方式：

1. 单独运行模型连通性脚本：

```powershell
python scripts\test_llm.py
```

2. 如果脚本失败，问题在模型配置或网络。
3. 如果脚本成功，但业务失败，问题更可能在 prompt、state 或工具。

### 5.6 工具调用失败

工具分三类排查。

状态流转工具：

- 重点看 `app/tools/state_transition.py`。
- 常见问题是参数格式不符合枚举值，例如交通方式必须是 `flight`、`train`、`driving`。
- 日期必须是 `YYYY-MM-DD`。

RAG 工具：

- 重点看 `app/tools/rag_tools.py`。
- 检查 `data/documents` 是否有文档。
- 检查 `data/vectorstore` 是否存在或能重新创建。

MCP 工具：

- 重点看 `app/mcp_core/client.py` 和 `app/tools/mcp_tools.py`。
- 检查外部 API key。
- 检查日志里的 `Skipping MCP server` 和 `failed to load tools`。

常用测试：

```powershell
pytest tests/test_mcp -q
pytest tests/test_rag -q
pytest tests/test_agents -q
```

### 5.7 RAG 检索结果不对

检查项：

- 原始文档是否存在于 `data/documents`。
- 文档是否被正确切分。
- 向量库是否过期。
- query 是否过泛或过窄。
- reranker、cache 是否影响结果。

常用命令：

```powershell
python scripts\load_documents.py
python scripts\init_rag.py
pytest tests/test_rag -q
```

如果怀疑向量库陈旧，可以重新初始化 RAG 数据，但操作前要确认不会覆盖用户需要保留的数据。

### 5.8 MCP 外部服务异常

检查项：

- 本地 stdio server 是否能启动。
- HTTP/streamable HTTP server 是否能访问。
- API key 是否配置。
- 工具 schema 是否被正常 normalize。
- 单个 MCP server 失败是否影响了全部工具加载。

相关配置：

- `AMAP_API_KEY`
- `VARIFLIGHT_API_KEY`
- `AIGOHOTEL_MCP_API`
- `MCP_TOOL_LOAD_TIMEOUT`

定位方式：

1. 看启动日志中的 enabled server。
2. 看哪些 server 被跳过。
3. 看是否所有 MCP server 都失败。
4. 优先隔离到某一个 server，再查它的 key、网络和返回 schema。

### 5.9 数据库或状态持久化异常

检查项：

- Postgres 是否启动。
- 数据库账号密码是否正确。
- 表是否创建。
- asyncpg 连接串是否正确。
- session 是否被异常回滚。
- checkpointer 是否可用。

常用命令：

```powershell
python scripts\init_db.py
pytest tests/test_api -q
```

重点文件：

- `app/models/base.py`
- `app/core/checkpointer.py`
- `app/core/store.py`
- `app/api/v1/chat.py`

## 6. 推荐排障顺序

遇到线上或本地问题时，建议按下面顺序走：

1. 复现问题，记录请求参数、conversation_id、用户、时间点。
2. 看 HTTP 状态码和 SSE 最后一个事件。
3. 查 `logs/error.log` 中同一时间点的异常栈。
4. 查 `logs/app.log` 中的 `trace_start`、`trace_end`、`trace_error`。
5. 判断问题属于 API、Agent、Graph、工具、RAG、MCP、数据库中的哪一层。
6. 跑对应测试目录。
7. 如果是外部依赖，单独验证 key、网络和服务返回。
8. 如果是状态问题，打印或检查当前 thread 的 `current_step` 与关键 state 字段。
9. 修复后增加或更新对应测试，避免同类问题再次回归。

## 7. 常见问题速查

| 现象 | 优先怀疑 | 下一步 |
| --- | --- | --- |
| 启动时直接报数据库连接失败 | Postgres 配置 | 查 `.env` 和 `app/models/base.py` |
| 启动慢或卡在 MCP | MCP 外部服务 | 查 `app/mcp_core/client.py` 和 MCP 日志 |
| 聊天接口 404 | conversation 归属 | 查 `Conversation` 数据 |
| 聊天接口返回 error 事件 | `generate_sse_stream()` 捕获异常 | 查 `logs/error.log` |
| Agent 每次忘记上下文 | checkpointer 降级或 thread_id 不一致 | 查 `get_checkpointer()` 和 `conversation_id` |
| 流程一直停在需求收集 | `record_requirement_tool` 未调用或参数不合法 | 查工具调用事件和 state |
| 选了目的地但没有进入交通规划 | `select_destination_tool` 未更新 `current_step` | 查 state transition |
| 工具调用后无响应 | MCP/RAG/外部 API 超时 | 查 tool 日志和对应测试 |
| RAG 搜不到内容 | 文档或向量库问题 | 重新初始化 RAG 并跑测试 |
| 有回复但历史为空 | assistant 内容为空或保存失败 | 查 `save_message()` 和数据库事务 |

## 8. 最小测试矩阵

每次改动后至少按影响范围运行对应测试。

```powershell
# API / 鉴权 / 会话
pytest tests/test_api -q

# Agent / LangGraph / 状态流转
pytest tests/test_agents -q

# MCP 工具
pytest tests/test_mcp -q

# RAG 检索
pytest tests/test_rag -q

# 行程和预算规划
pytest tests/test_planner -q

# 全量回归
pytest -q
```

## 9. 建议补充的观测点

当前项目已经有日志和 trace，但后续可以继续增强：

- 给每个 SSE 请求增加 `request_id`，贯穿 API、Agent、工具、数据库日志。
- 在每个 step 开始时记录 `current_step` 和关键 state 字段是否存在，不记录敏感内容。
- 在每次工具调用结束时记录耗时、工具名、是否成功、输出摘要长度。
- 对 MCP 工具加载增加 server 级别健康状态。
- 对 RAG 增加命中文档数、top score、向量库版本或构建时间。
- 对数据库写入失败增加 conversation_id、role、content length。

## 10. 一句话定位法

先看 SSE 最后停在哪个事件，再用日志中的 `trace_error` 对齐具体步骤，最后到对应文件和测试目录验证。

