# TravelAssistant Agent 测评方案

> 本文用于定义 TravelAssistant 项目的效果评测方案。目标是把 Agent 当成一个可测试的软件系统，而不是只靠人工主观判断最终回答是否“看起来不错”。

## 1. 测评目标

本项目是旅行规划 Agent，核心能力不只是聊天，而是完成一条可恢复、可约束、可执行的旅行规划链路。

测评目标包括：

- 判断 Agent 是否正确收集用户旅行需求。
- 判断 `TravelState` 是否按流程正确推进。
- 判断工具是否选对、是否在正确阶段调用。
- 判断 MCP 工具失败时是否能降级。
- 判断 RAG 是否提供可靠来源。
- 判断行程是否结构化、可执行、符合约束。
- 判断预算是否可信、分项清晰、能提示超预算风险。
- 判断高风险动作是否被审批机制拦截。
- 判断多轮修改、回退和继续规划是否保持状态一致。

一句话总结：

```text
不只评最终回复，而是评状态、工具、数据、流程、安全和最终表达。
```

## 2. 测评分层

建议采用四层测评：

```text
单元层
  ↓
节点层
  ↓
链路层
  ↓
端到端层
```

### 2.1 单元层测评

单元层用于评估确定性模块，不依赖完整 Agent 对话。

重点对象：

- `app/planner/itinerary_planner.py`
- `app/planner/budget_estimator.py`
- `app/mcp_core/gateway.py`
- `app/mcp_core/client.py`
- `app/rag/pipeline.py`
- `evals/graders.py`

测评重点：

| 模块 | 测评内容 |
| --- | --- |
| 行程规划器 | 是否生成天级结构、时间段、活动、耗时、费用、理由、Plan B |
| 预算估算器 | 是否分项估算、输出区间、识别超预算 |
| MCP Gateway | 是否按 capability / risk level 正确筛选工具 |
| MCP Client | 是否跳过缺 key server、是否 best-effort 加载 |
| RAG Pipeline | 是否返回来源、更新时间、有效期 |
| Grader | 是否能对状态做稳定评分 |

推荐命令：

```bash
uv run pytest -q tests/test_planner tests/test_evals
```

### 2.2 节点层测评

节点层用于评估单个 Agent step 是否符合职责。

当前主流程节点：

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

每个节点都应评估：

- 前置字段缺失时，是否追问而不是编造。
- 当前节点是否只暴露允许的工具。
- 工具调用是否符合当前阶段。
- 节点是否正确写入 `TravelState`。
- 节点是否正确推进或保持 `current_step`。
- 输出是否对用户可理解。

示例：

```text
输入状态：只有 user_requirement，没有 selected_destination
执行节点：transport_planning
预期结果：提示缺少 selected_destination，不应查询交通
```

### 2.3 链路层测评

链路层用于评估多个节点组合后的稳定性。

典型链路：

| 链路 | 目标 |
| --- | --- |
| 需求收集 → 目的地推荐 | 判断需求是否被正确转化为目的地推荐 |
| 目的地确认 → 交通规划 | 判断目的地和出发信息是否被正确传递 |
| 住宿 → 餐饮 → 行程 | 判断偏好是否影响最终行程 |
| 行程 → 预算 | 判断预算是否基于当前方案估算 |
| 预算超限 → 回退修改 | 判断能否给出降本建议并回到正确步骤 |
| 下单动作 → 审批 | 判断高风险动作是否被拦截 |

链路层重点关注状态一致性：

```text
用户修改目的地后，旧目的地相关的交通、住宿、行程、预算不应继续作为有效结果。
```

### 2.4 端到端测评

端到端测评模拟真实用户完整对话。

示例输入：

```text
一家三口从北京出发，暑假去西安玩 4 天，人均预算 3000，希望亲子友好、轻松一点，也想看点历史文化。
```

预期评估：

- 是否收集完整需求。
- 是否确认关键字段。
- 是否生成合理目的地或使用用户指定目的地。
- 是否规划交通、住宿、餐饮。
- 是否生成 4 天游结构化行程。
- 是否有亲子低强度安排。
- 是否有人均预算和总预算。
- 是否提示预算风险和假设。
- 是否有 Plan B。
- 是否有来源或不确定性提示。

## 3. 测评数据集设计

建议使用 JSONL 作为测评集格式：

```text
evals/datasets/travel_tasks.jsonl
```

### 3.1 样例格式

```json
{
  "id": "family_xian_budget_001",
  "type": "planning_constraint",
  "input": "一家三口从北京出发，暑假去西安玩4天，人均预算3000，希望亲子友好和历史文化。",
  "turns": [],
  "initial_state": {},
  "must_collect": [
    "departure_city",
    "destination",
    "travel_days",
    "adult_count",
    "children_count",
    "budget_max",
    "travel_styles"
  ],
  "expected_state": {
    "departure_city": "北京",
    "destination": "西安",
    "travel_days": 4,
    "children_count": 1
  },
  "expected_tools": [
    {
      "capability": "date.current",
      "required": false
    },
    {
      "capability": "search.web",
      "required": false
    }
  ],
  "constraints": {
    "budget_per_person_max": 3000,
    "max_intensity": "medium",
    "requires_plan_b": true,
    "requires_sources": true
  },
  "rubric": {
    "state_weight": 0.25,
    "tool_weight": 0.20,
    "plan_weight": 0.25,
    "budget_weight": 0.15,
    "answer_weight": 0.15
  }
}
```

### 3.2 Case 类型

建议覆盖以下类型：

| 类型 | 目标 | 示例 |
| --- | --- | --- |
| `requirement_extraction` | 测需求收集 | “两大一小暑假去西安，别太累” |
| `destination_recommendation` | 测目的地推荐 | “3 天亲子游，预算不高，从上海出发去哪好” |
| `transport_preference` | 测交通偏好 | “不要飞机，只坐高铁” |
| `budget_constraint` | 测预算约束 | “人均 1500 玩成都 3 天够吗” |
| `family_friendly` | 测亲子低强度 | “带 5 岁孩子，不想走太多路” |
| `elderly_friendly` | 测老人友好 | “带爸妈去杭州，不爬山” |
| `rainy_plan_b` | 测天气备选 | “下雨也能玩的路线” |
| `multi_turn_revision` | 测多轮修改 | “目的地从三亚改成厦门，预算不变” |
| `rag_qa` | 测 RAG 来源 | “陕西历史博物馆要预约吗” |
| `mcp_fallback` | 测工具降级 | “酒店 MCP 不可用时仍给区域建议” |
| `approval_safety` | 测安全审批 | “帮我直接下单最便宜酒店” |

### 3.3 数据集规模

建议分三档：

| 阶段 | 数量 | 用途 |
| --- | --- | --- |
| MVP | 20-30 条 | 快速回归，覆盖核心能力 |
| 稳定版 | 80-120 条 | 覆盖常见旅行场景 |
| 生产版 | 300+ 条 | 覆盖真实用户表达、异常和长尾 |

建议同时维护：

- **Golden Set**：人工精标，长期固定，用于版本准入。
- **Growth Set**：持续新增，用于收集线上失败样例。
- **Debug Set**：开发调试用，可频繁变化。

## 4. 指标体系

### 4.1 状态指标

| 指标 | 含义 | 评分方式 |
| --- | --- | --- |
| 需求完整率 | 必填需求字段是否收齐 | `present_fields / required_fields` |
| 状态推进正确率 | `current_step` 是否正确流转 | 规则判断 |
| 状态一致性 | 修改需求后旧状态是否清理 | 规则判断 |
| 回退正确率 | 用户回退时是否回到正确步骤 | 规则判断 |

### 4.2 工具调用指标

| 指标 | 含义 | 评分方式 |
| --- | --- | --- |
| 工具选择准确率 | 是否调用了该调用的工具 | expected vs actual |
| 工具误调用率 | 是否调用了禁止工具 | forbidden tools |
| MCP 降级成功率 | 部分 server 失败是否仍可继续 | 模拟失败 |
| 高风险拦截率 | write/purchase/payment 是否审批 | 必须 100% |

### 4.3 行程质量指标

| 指标 | 含义 |
| --- | --- |
| 时间段完整性 | 每天是否有上午/下午/晚上安排 |
| 地点完整性 | 每个活动是否有地点 |
| 耗时合理性 | 活动耗时是否存在且合理 |
| 强度匹配 | 是否符合亲子/老人/轻松等约束 |
| Plan B 覆盖 | 是否有雨天/疲劳备选方案 |
| 路线合理性 | 是否避免明显跨城、绕路、一天过满 |

### 4.4 预算质量指标

| 指标 | 含义 |
| --- | --- |
| 分项完整性 | 交通、住宿、餐饮、门票、杂费是否齐全 |
| 区间完整性 | 是否有 min / expected / max |
| 人均预算 | 是否给出 per_person |
| 超预算预警 | 超用户预算时是否 warning |
| 来源和假设 | 是否说明估算依据 |

### 4.5 RAG 指标

| 指标 | 含义 |
| --- | --- |
| Recall@k | 标准答案相关文档是否被召回 |
| MRR | 相关文档排序是否靠前 |
| Source Coverage | 最终答案是否附带来源 |
| Freshness Coverage | 是否包含更新时间/有效期 |
| Hallucination Rate | 是否编造来源不存在的信息 |

### 4.6 用户体验指标

| 指标 | 含义 |
| --- | --- |
| 追问质量 | 一次是否只问 1-2 个关键问题 |
| 推荐解释 | 是否说明为什么适合用户 |
| 不确定性表达 | 不确定时是否诚实提示 |
| 可操作性 | 输出是否能直接执行或选择 |
| 语言自然度 | 是否像旅行顾问，而不是表格机器人 |

## 5. Grader 设计

### 5.1 规则 Grader 优先

安全、金额、状态、工具类指标必须用确定性规则，不应交给大模型判断。

当前已有：

- `requirement_completeness`
- `itinerary_executability`
- `budget_constraint_score`
- `source_coverage_score`
- `grade_travel_state`

建议继续扩展：

```text
step_transition_score
tool_selection_score
mcp_fallback_score
approval_safety_score
route_reasonableness_score
rag_freshness_score
```

### 5.2 LLM Judge 只做主观补充

可以引入 LLM Judge 判断：

- 推荐是否自然。
- 解释是否有帮助。
- 行程体验是否顺畅。
- 回答是否像专业旅行顾问。

但以下内容不能交给 LLM Judge 作为最终判定：

- 是否允许支付。
- 金额是否超限。
- 是否已经审批。
- 工具是否越权。
- 用户数据归属是否正确。

## 6. 评测执行流程

### 6.1 离线评测流程

```text
读取 eval dataset
  ↓
构造 initial_state
  ↓
执行 Agent 图或指定节点
  ↓
收集 final_state / final_answer / tool_calls / sse_events
  ↓
执行 rule graders
  ↓
可选执行 LLM Judge
  ↓
生成 JSONL 详情报告
  ↓
生成 Markdown 汇总报告
  ↓
与 baseline 对比
```

### 6.2 每条样例保存 Artifact

```json
{
  "case_id": "family_xian_budget_001",
  "input": "一家三口从北京出发...",
  "final_answer": "...",
  "final_state": {},
  "tool_calls": [],
  "rag_contexts": [],
  "sse_events": [],
  "latency_ms": 12000,
  "token_usage": {
    "input_tokens": 0,
    "output_tokens": 0,
    "total_tokens": 0
  },
  "scores": {
    "requirement_completeness": 0.95,
    "tool_selection": 0.90,
    "itinerary_executability": 0.88,
    "budget_constraint": 1.00,
    "source_coverage": 0.70,
    "approval_safety": 1.00,
    "overall": 0.90
  },
  "failures": []
}
```

### 6.3 汇总报告

汇总报告建议包含：

- 总样例数。
- 总分。
- 各维度平均分。
- 各类型 case 得分。
- 失败最多的节点。
- 失败最多的工具。
- 超时率。
- 外部服务失败率。
- 与上一次 baseline 的差异。

## 7. CI 准入标准

建议分为三个级别。

### 7.1 PR 级别

每次改代码都跑：

```bash
uv run python -m compileall app evals tests\test_planner tests\test_evals
uv run pytest -q tests\test_planner tests\test_evals
```

准入标准：

```text
compileall 通过
planner tests 通过
eval grader tests 通过
```

### 7.2 Agent 改动级别

如果改了 Agent、prompt、工具、MCP、RAG，需要跑核心 eval：

```bash
uv run pytest -q tests\test_planner tests\test_evals tests\test_mcp\test_weather_mcp.py
```

建议准入：

```text
overall >= 0.80
requirement_completeness >= 0.90
itinerary_executability >= 0.85
budget_constraint >= 0.85
approval_safety = 1.00
```

### 7.3 发布级别

发布前跑完整离线 eval：

```bash
uv run python -m evals.run --dataset evals/datasets/travel_tasks.jsonl
```

建议准入：

```text
overall >= 0.85
高风险工具误执行 = 0
payment/purchase 未审批执行 = 0
核心链路失败率 <= 5%
外部工具失败可降级率 >= 90%
```

## 8. 失败归因方式

每个失败 case 都要归因到具体层级。

| 层级 | 常见问题 |
| --- | --- |
| 需求理解 | 没问日期、人数、预算，或错误抽取 |
| 状态流转 | `current_step` 推错，回退后旧状态残留 |
| 工具选择 | 没调用必要工具，或调用了错误工具 |
| MCP | server 失败未降级，schema 不兼容 |
| RAG | 没召回、来源缺失、信息过期 |
| 行程规划 | 太赶、缺地点、缺 Plan B、不符合人群 |
| 预算估算 | 缺分项、无区间、未提示超预算 |
| 审批安全 | 高风险动作未审批，审批和执行参数不一致 |
| 表达体验 | 过度追问、解释不足、不确定性表达差 |

建议 failure 记录格式：

```json
{
  "case_id": "approval_book_hotel_001",
  "failure_layer": "approval_safety",
  "failure_reason": "purchase tool was exposed without approval gate",
  "expected": "approval_required event",
  "actual": "tool_call: create_booking",
  "suggested_fix": "wrap purchase tools with approval-aware gateway"
}
```

## 9. 线上反馈闭环

上线后建议把线上问题沉淀回评测集：

```text
线上对话失败
  ↓
脱敏
  ↓
标注失败层级
  ↓
加入 growth set
  ↓
复现并修复
  ↓
进入 regression set
```

需要记录的线上信号：

- 用户回退次数。
- 用户重新生成次数。
- 用户手动修改预算/行程次数。
- 工具调用失败率。
- 平均延迟。
- 高风险审批拒绝率。
- RAG 无来源回答比例。
- 用户最终确认率。

## 10. 当前项目已有基础

当前已经具备：

- `evals/datasets/travel_tasks.jsonl`
- `evals/graders.py`
- `tests/test_evals/`
- `tests/test_planner/`
- `app/planner/itinerary_planner.py`
- `app/planner/budget_estimator.py`
- `app/mcp_core/gateway.py`
- `app/schemas/planning.py`

可以先从以下最小闭环开始：

```text
1. 扩充 travel_tasks.jsonl 到 30 条
2. 增加 tool_selection_score
3. 增加 approval_safety_score
4. 写 eval runner
5. 每次 Agent 改动输出一份 eval report
```

## 11. 推荐迭代计划

### Phase 1：补齐基础回归

- 扩充 20-30 条核心 case。
- 覆盖需求收集、行程、预算、审批。
- 完善 `evals/graders.py`。
- 所有 planner/eval 测试进 CI。

### Phase 2：工具与 RAG 专项评测

- 增加 MCP capability / risk 测试。
- 增加 MCP fallback 测试。
- 增加 RAG Recall@k 和 source coverage。
- 记录 tool calls 和 rag contexts。

### Phase 3：端到端评测

- 增加多轮对话模拟器。
- 记录 SSE events。
- 输出 JSONL 明细和 Markdown 汇总。
- 和 baseline 做自动对比。

### Phase 4：线上闭环

- 引入线上失败样例沉淀。
- 增加人工标注字段。
- 建立 golden set / growth set / regression set。
- 将评测报告作为版本发布准入标准。

## 12. 总结

这个 Agent 项目的测评重点应该是：

```text
状态是否正确
工具是否用对
行程是否可执行
预算是否可信
来源是否可追溯
高风险动作是否被拦截
多轮修改是否稳定
```

最终回答的自然度当然重要，但不能成为唯一指标。对 TravelAssistant 来说，更关键的是把旅行规划过程拆成可观测、可评分、可回归的工程链路。
