# 旅行助手优化开发计划

> 本计划基于 `docs/optimization.md` 拆解，目标是将当前旅行助手从「Agent 调工具生成文本」升级为「可恢复、可评测、可约束规划、可产品化交互」的旅行规划系统。

## 1. 开发目标

### 1.1 核心目标

- 主流程稳定：旅行规划流程由显式图控制，减少模型跳步骤和误调用工具。
- 状态可恢复：会话中断、服务重启后可以继续规划。
- 数据结构化：交通、酒店、天气、POI、预算、行程都以结构化数据流转。
- 规划可信：预算和行程基于真实数据、约束和来源，而不是硬编码估算。
- 质量可评测：RAG、Agent、工具调用和最终结果都有可回放、可对比的评测方式。

### 1.2 非目标

- 第一阶段不重做完整前端。
- 第一阶段不接入真实支付和正式下单。
- 第一阶段不一次性替换所有模型提供商。
- 第一阶段不大规模重构数据库模型，优先复用现有结构。

## 2. 总体阶段

| 阶段 | 名称 | 建议周期 | 目标 |
| --- | --- | --- | --- |
| Phase 1 | 主流程稳定化 | 1 周 | 显式图、Postgres checkpoint、结构化输出 |
| Phase 2 | 规划质量升级 | 2-3 周 | 约束行程、真实预算、RAG 引用 |
| Phase 3 | 工具体系升级 | 2-4 周 | MCP 工具网关、权限、审计、降级 |
| Phase 4 | 体验增强 | 3-6 周 | 地图/时间轴事件、多模态入口 |
| Phase 5 | 评测与观测 | 持续 | Agent eval、RAG eval、trace、成本监控 |

## 3. Phase 1：主流程稳定化

### 3.1 显式旅行规划图

目标：

- 使用 `LangGraph StateGraph` 承载主旅行规划流程。
- 将当前大而全的 Agent 拆成明确节点。

主要任务：

- 新增 `app/agents/graphs/travel_planner_graph.py`。
- 定义节点：
  - `collect_requirement`
  - `recommend_destination`
  - `confirm_destination`
  - `plan_transport`
  - `plan_accommodation`
  - `plan_food`
  - `generate_itinerary`
  - `summarize_budget`
  - `final_confirmation`
- 将 `current_step` 的推进逻辑从工具调用迁移到图节点。
- 保留现有回退工具，但让回退逻辑更新图状态，而不是依赖模型自由判断。
- 每个节点只暴露当前阶段需要的工具。

涉及文件：

- `app/agents/handoffs/travel_agent.py`
- `app/agents/handoffs/step_config.py`
- `app/tools/state_transition.py`
- `app/core/state.py`
- 新增 `app/agents/graphs/travel_planner_graph.py`

验收标准：

- 用户可以从需求收集走到预算汇总，状态按固定节点推进。
- 任意节点缺少前置字段时返回明确错误，而不是让模型继续编造。
- 回退到目的地、交通、住宿、餐饮、行程步骤后，后续状态会被正确清理。
- 现有核心测试不因流程迁移而大面积失效。

风险：

- 当前工具函数里混合了状态更新和用户提示，迁移时容易重复写入消息。
- 需要兼容现有测试中对 `create_agent` 返回结构的假设。

### 3.2 Postgres Checkpoint 接入主流程

目标：

- 主流程不再使用内存 `MemorySaver`。
- 会话状态可以跨请求、跨服务重启恢复。

主要任务：

- 在主 Agent/Graph 创建处使用 `get_checkpointer()`。
- 确认应用启动生命周期初始化 `app/core/checkpointer.py`。
- 保持 `conversation_id` 作为 `configurable.thread_id`。
- 补充 checkpoint 初始化失败时的降级和错误提示。

涉及文件：

- `app/agents/handoffs/travel_agent.py`
- `app/core/checkpointer.py`
- `app/main.py`
- `app/api/v1/chat.py`
- `tests/test_agents`

验收标准：

- 同一个 `conversation_id` 多次请求可以读取上一轮状态。
- 服务重启后，已保存会话可以继续。
- 数据库不可用时返回可理解错误，不静默丢状态。

风险：

- 本地开发依赖 Postgres 环境，测试需要提供 mock 或测试库。
- checkpoint schema 初始化可能和当前数据库初始化流程有顺序问题。

### 3.3 结构化数据模型

目标：

- 中间结果不再只传自然语言。
- 前端和后续计算可以直接使用结构化对象。

主要任务：

- 新增或扩展 schema：
  - `TravelRequirement`
  - `DestinationOption`
  - `TransportOption`
  - `HotelOption`
  - `FoodOption`
  - `WeatherForecast`
  - `PoiOption`
  - `ItineraryItem`
  - `ItineraryPlan`
  - `BudgetBreakdown`
  - `SourceReference`
- 工具返回结构化结果，同时保留 markdown summary 字段。
- 在 `TravelState` 中保存结构化候选项。

涉及文件：

- `app/core/state.py`
- `app/schemas/state.py`
- `app/schemas/response.py`
- `app/tools/transport_query.py`
- `app/tools/router_query.py`
- `app/tools/rag_tools.py`

验收标准：

- 交通、目的地、行程、预算输出均可 JSON 序列化。
- 状态中的候选项可以被排序、过滤和二次计算。
- SSE 仍能输出可读文本，不破坏现有调用方。

风险：

- Pydantic 模型和 LangGraph state 类型需要保持兼容。
- 部分 MCP 工具返回不可控，需要先做适配层。

## 4. Phase 2：规划质量升级

### 4.1 约束式行程规划

目标：

- 替换模板行程生成。
- 输出可执行、可解释、可调整的行程。

主要任务：

- 新增 `app/planner` 目录。
- 实现 POI 评分：
  - 偏好匹配。
  - 距离成本。
  - 开放时间。
  - 天气适配。
  - 人群强度。
  - 预算影响。
- 实现日程分配：
  - 每天上午/下午/晚上分段。
  - 餐点时间固定窗口。
  - 景点间交通耗时估算。
  - 每天活动强度上限。
- 输出多方案：
  - 省钱版。
  - 舒适版。
  - 深度游版。

涉及文件：

- `app/tools/state_transition.py`
- 新增 `app/planner/itinerary_planner.py`
- 新增 `app/planner/scoring.py`
- 新增 `app/planner/constraints.py`
- 新增 `app/planner/models.py`

验收标准：

- 每个行程项包含地点、时间段、预计耗时、预计费用、推荐原因和来源。
- 同一天内路线顺序基本符合地理距离。
- 超预算、恶劣天气、营业时间冲突时有明确提示。

风险：

- 缺少真实 POI 数据时，需要先提供 mock/fallback。
- 约束过多可能导致无可行方案，需要设计降级策略。

### 4.2 真实预算估算

目标：

- 替换硬编码预算。
- 给出区间预算和来源。

主要任务：

- 新增 `app/planner/budget_estimator.py`。
- 定义预算项：
  - 大交通。
  - 市内交通。
  - 住宿。
  - 餐饮。
  - 门票。
  - 保险。
  - 杂费。
- 为每项预算保存：
  - `min_amount`
  - `expected_amount`
  - `max_amount`
  - `source`
  - `estimated`
  - `updated_at`
- 对数据缺失场景提供城市级 fallback。

涉及文件：

- `app/tools/state_transition.py`
- `app/core/state.py`
- 新增 `app/planner/budget_estimator.py`

验收标准：

- 预算输出包含乐观、正常、保守三档。
- 如果使用估算值，结果中明确标记。
- 预算超过用户上限时给出调优建议。

风险：

- 外部价格 API 可能不稳定。
- 酒店和机票价格实时性强，需要标记更新时间。

### 4.3 RAG 引用和时效

目标：

- RAG 结果可追溯。
- 过期信息可识别。

主要任务：

- 扩展文档 metadata：
  - `source_url`
  - `source_title`
  - `city`
  - `category`
  - `updated_at`
  - `valid_until`
- RAG 返回 `RetrievedContext`。
- 最终回答中附带关键引用来源。
- 对过期或未知更新时间的信息加提示。

涉及文件：

- `app/rag/pipeline.py`
- `app/rag/retriever.py`
- `app/rag/document_loader.py`
- `app/tools/rag_tools.py`
- `scripts/load_documents.py`

验收标准：

- 每条 RAG 证据都有来源 metadata。
- 门票、营业时间、交通政策类答案能提示时效风险。
- RAG 工具输出可用于 eval。

风险：

- 现有文档可能缺少来源和更新时间，需要补数据。
- 向量库需要重新构建。

## 5. Phase 3：工具体系升级

### 5.1 MCP 工具网关

目标：

- 在 MCP Client 之上统一管理工具能力。
- 避免直接把所有工具裸挂给 Agent。

主要任务：

- 新增 `app/mcp_core/gateway.py`。
- 为工具注册能力标签：
  - `weather.realtime`
  - `map.poi`
  - `transport.train`
  - `transport.flight`
  - `hotel.search`
  - `search.web`
- 统一工具调用接口：
  - 输入校验。
  - 超时控制。
  - 重试。
  - fallback。
  - 结果归一化。
  - 调用日志。
- 工具按当前节点动态暴露。

涉及文件：

- `app/mcp_core/client.py`
- `app/tools/mcp_tools.py`
- 新增 `app/mcp_core/gateway.py`
- 新增 `app/mcp_core/registry.py`

验收标准：

- MCP server 部分失败时，系统仍能使用其他可用工具。
- 工具调用失败有明确错误类型。
- 工具返回结果统一映射到内部 schema。

风险：

- 不同 MCP server 的返回格式差异较大。
- 部分第三方 MCP 服务稳定性不可控。

### 5.2 权限和人工确认

目标：

- 高风险操作必须由用户确认。
- 查询类工具自动执行，写入/下单/支付类工具中断等待确认。

主要任务：

- 给工具增加 `risk_level`。
- 扩展 SSE 事件 `approval_required`。
- 设计确认 payload：
  - 操作类型。
  - 供应商。
  - 金额。
  - 取消政策。
  - 用户信息。
  - 确认后执行的 tool call。
- 用户确认后恢复图执行。

涉及文件：

- `app/tools/approval_tools.py`
- `app/api/v1/chat.py`
- `app/core/state.py`
- `app/agents/graphs/travel_planner_graph.py`

验收标准：

- 查询工具不阻塞。
- 下单/支付类工具不会在用户确认前执行。
- 用户拒绝后流程能回到可修改状态。

风险：

- 需要明确工具风险分类，否则容易漏拦截。
- 前端需要配合展示确认卡片。

## 6. Phase 4：产品体验增强

### 6.1 扩展 SSE 事件协议

目标：

- 后端不只输出 token，还能输出结构化增量事件。

主要任务：

- 定义 SSE 事件类型：
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
- 每个事件统一包含：
  - `type`
  - `conversation_id`
  - `step`
  - `payload`
  - `timestamp`
- 后端在工具结果和图节点完成时发送结构化事件。

涉及文件：

- `app/api/v1/chat.py`
- `app/schemas/response.py`
- `app/agents/graphs/travel_planner_graph.py`

验收标准：

- 旧的 token 流仍可用。
- 新前端可以根据事件渲染地图、卡片、预算面板。
- 错误事件包含可展示信息和 debug id。

风险：

- 事件过多可能导致前端状态复杂，需要定义稳定协议。

### 6.2 图片和语音输入

目标：

- 支持用户从截图和语音中输入旅行需求。

主要任务：

- 新增图片解析接口：
  - 酒店截图。
  - 攻略截图。
  - 聊天记录截图。
  - 景点图片。
- 图片解析结果进入需求确认节点。
- 新增语音转文本入口。
- 语音文本进入同一需求收集流程。

涉及文件：

- 新增 `app/api/v1/uploads.py`
- 新增 `app/tools/vision_tools.py`
- 新增 `app/tools/audio_tools.py`
- `app/core/state.py`

验收标准：

- 用户上传截图后，系统能提取地点、价格、日期、酒店名等候选字段。
- 图片/语音识别结果不会直接写入最终计划，必须经过用户确认。

风险：

- 多模态模型成本较高，需要控制调用时机。
- OCR 和视觉理解可能出错，需要用户确认机制兜底。

## 7. Phase 5：评测与观测

### 7.1 Agent Eval

目标：

- 建立可回归的 Agent 质量评测。

主要任务：

- 新增 `evals` 或 `tests/evals`。
- 构建任务集：
  - 亲子游。
  - 周末短途游。
  - 学生低预算游。
  - 老人轻松游。
  - 美食优先游。
  - 雨天备选。
  - 中途改目的地。
  - 中途改预算。
  - 指定交通方式。
- 定义评分指标：
  - 需求字段完整率。
  - 工具调用正确率。
  - 预算是否超限。
  - 行程是否可执行。
  - 引用覆盖率。
  - 是否出现明显幻觉。

涉及文件：

- 新增 `tests/evals/test_travel_planning_eval.py`
- 新增 `evals/datasets/travel_tasks.jsonl`
- 新增 `evals/graders.py`

验收标准：

- 每次模型/prompt/工具改动后可以跑一组基础 eval。
- 评测结果能输出 pass rate 和失败案例。

风险：

- 自动评分难以覆盖所有体验问题，需要人工抽样复核。

### 7.2 Trace 和成本监控

目标：

- 每次规划过程可复盘。
- 成本、延迟和错误可观测。

主要任务：

- 完善 LangSmith trace metadata。
- 记录每个节点：
  - 输入。
  - 输出摘要。
  - 耗时。
  - 模型名称。
  - token。
  - 工具调用。
  - 错误。
- 记录用户行为：
  - 确认。
  - 回退。
  - 重新生成。
  - 锁定方案。
  - 删除行程项。
- 建立关键指标：
  - 规划成功率。
  - 平均规划耗时。
  - 工具失败率。
  - 用户回退率。
  - 平均 token 成本。

涉及文件：

- `app/utils/logger.py`
- `app/core/middleware.py`
- `app/api/v1/chat.py`
- 新增 `app/observability/tracing.py`

验收标准：

- 任意失败规划可以通过 `conversation_id` 追踪到失败节点。
- 可以统计最近一段时间工具失败率和平均耗时。

风险：

- 日志中不能泄露用户敏感信息。
- trace 数据量可能增长较快，需要采样或保留策略。

## 8. 推荐迭代顺序

### Iteration 1：状态和流程底座

交付内容：

- 显式旅行规划图初版。
- Postgres checkpoint 接入。
- 基础结构化 state。
- 保持现有 SSE 文本输出。

完成标志：

- 可以完成一条完整旅行规划链路。
- 中断后可以恢复。
- 回退步骤可用。

### Iteration 2：结构化工具和预算

交付内容：

- 交通、目的地、天气、酒店工具结构化输出。
- `BudgetEstimator` 初版。
- 硬编码预算替换。

完成标志：

- 预算明细包含来源和估算标记。
- 预算超限时有调整建议。

### Iteration 3：行程规划器

交付内容：

- POI/餐厅/酒店候选收集。
- 基础评分和约束规划。
- 多方案输出。

完成标志：

- 行程包含时间、地点、费用、来源和推荐理由。
- 能生成至少 2 个候选方案。

### Iteration 4：RAG 和 MCP 可信化

交付内容：

- RAG 引用和时效 metadata。
- MCP 工具网关。
- 工具调用日志、超时和 fallback。

完成标志：

- 关键回答有引用。
- 单个 MCP 服务失败不会拖垮整个流程。

### Iteration 5：评测闭环

交付内容：

- Agent eval 数据集。
- RAG eval 基础用例。
- trace metadata 完善。

完成标志：

- 可以对模型/prompt/工具变更跑回归评测。
- 可以定位失败规划的具体节点和工具调用。

### Iteration 6：产品体验增强

交付内容：

- 扩展 SSE 事件协议。
- 地图/时间轴/预算面板数据输出。
- 图片和语音输入 PoC。

完成标志：

- 前端可以根据结构化事件渲染非文本结果。
- 图片/语音输入结果进入用户确认流程。

## 9. 任务依赖关系

```text
Postgres checkpoint
        |
        v
显式旅行规划图 ---> 结构化 state/schema ---> 结构化工具输出
        |                       |                   |
        v                       v                   v
回退/恢复能力              预算估算器          MCP 工具网关
        |                       |                   |
        v                       v                   v
人工确认机制              行程规划器          工具审计/降级
        \                       |                   /
         \                      v                  /
          -------> Agent/RAG Eval + Trace <-------
                              |
                              v
                    地图/时间轴/多模态体验
```

## 10. 测试计划

### 10.1 单元测试

- State 初始化和更新。
- 每个图节点的前置条件校验。
- 回退后后续字段清理。
- BudgetEstimator 区间计算。
- ItineraryPlanner 约束冲突处理。
- MCP 工具网关 timeout/fallback。

### 10.2 集成测试

- 完整旅行规划流程。
- 中途修改目的地。
- 中途修改预算。
- 外部工具失败后的降级。
- checkpoint 恢复。
- approval_required 后恢复执行。

### 10.3 评测测试

- 固定旅行任务集回归。
- RAG 检索命中率。
- 引用覆盖率。
- 工具调用正确率。
- 预算是否超过用户约束。

## 11. 风险和应对

| 风险 | 表现 | 应对 |
| --- | --- | --- |
| 流程迁移影响现有测试 | 大量测试假设旧 Agent 行为 | 先兼容旧入口，再逐步替换内部实现 |
| 外部 MCP 服务不稳定 | 工具超时或空结果 | 工具网关增加 timeout、fallback、健康检查 |
| 结构化 schema 过早定死 | 后续字段扩展困难 | 使用核心字段必填、扩展字段 metadata 的方式 |
| 约束规划太复杂 | 无法快速交付 | 先做启发式评分和简单时间窗，再逐步增强 |
| 评测指标不准确 | 自动评分误判 | 自动评测 + 人工抽样复核 |
| 日志泄露隐私 | trace 中包含用户敏感信息 | 增加脱敏和字段白名单 |

## 12. 最小可行版本

如果只做一个最小可行升级版本，建议包含：

- 主流程显式图。
- Postgres checkpoint。
- 交通/目的地/预算结构化输出。
- 替换硬编码预算。
- 简单行程约束规划。
- 10 条 Agent eval 用例。

这个版本已经能明显区别于普通旅行聊天机器人，也能体现工程上的可恢复、可测试和可信规划能力。
