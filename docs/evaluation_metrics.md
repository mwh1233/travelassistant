可以按“分层评测 + 回归集 + trace 复盘”来做。这个项目不要只评最终回答文本，要重点评 **Agent 状态、工具调用、行程结构、预算、RAG 来源和安全审批**。

## 1. 评测分层

### 1. 单元层

评确定性模块是否靠谱：

- 行程规划器：是否生成每天上午/下午/晚上安排、是否有耗时、费用、理由、Plan B。
- 预算估算器：是否按交通、住宿、餐饮、门票、杂费拆分，是否超预算预警。
- MCP Gateway：是否按 capability / risk level 正确筛工具。
- RAG pipeline：是否返回来源、更新时间、有效期。

当前已有雏形：
- `app/planner/itinerary_planner.py:74`
- `app/planner/budget_estimator.py:53`
- `evals/graders.py:25`

### 2. 节点层

单独评每个 Agent step：

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

每个节点评：

- 输入状态是否满足前置条件。
- 缺字段时是否追问，而不是编造。
- 是否只调用当前阶段允许的工具。
- 是否正确更新 `TravelState.current_step`。
- 是否写入正确结构化字段。

### 3. 端到端层

模拟完整用户需求：

```text
“一家三口从北京出发，五一去西安 4 天，人均预算 3000，想轻松亲子和文化体验”
```

评最终结果是否：

- 收集完整需求。
- 推荐合理目的地。
- 交通、住宿、餐饮选择匹配需求。
- 行程可执行。
- 预算在用户上限附近或给出降本建议。
- 有来源和风险提示。
- 多轮修改后状态不乱。

## 2. 评测数据集怎么设计

建议放在 `evals/datasets/travel_tasks.jsonl:1`，每条 case 包含：

```json
{
  "id": "family_xian_001",
  "type": "planning_constraint",
  "input": "一家三口从北京出发，去西安玩4天，人均预算3000，想亲子友好和文化体验",
  "must_collect": [
    "departure_city",
    "destination",
    "travel_days",
    "adult_count",
    "children_count",
    "budget_max",
    "travel_styles"
  ],
  "expected_tools": [
    "date.current",
    "search.web",
    "transport.train"
  ],
  "constraints": {
    "budget_per_person_max": 3000,
    "max_intensity": "medium",
    "requires_plan_b": true,
    "requires_sources": true
  }
}
```

建议 case 类型覆盖：

- 需求收集
- 目的地推荐
- 指定交通方式
- 低预算旅行
- 亲子/老人低强度
- 雨天 Plan B
- 多轮修改目的地/预算
- RAG 问答
- 工具失败降级
- 下单/支付审批安全

## 3. 核心指标

### Agent 状态指标

- 需求字段完整率
- `current_step` 推进正确率
- 回退后状态清理正确率
- 多轮修改后状态一致性

### 工具调用指标

- 工具选择准确率
- 不该调用工具的误调用率
- MCP 降级成功率
- 高风险工具拦截率

### 行程质量指标

- 每天是否有明确时间段
- 每个活动是否有地点、耗时、费用、理由
- 是否有 Plan B
- 是否符合亲子/老人强度
- 是否避免明显跨城/绕路安排

### 预算指标

- 是否分项
- 是否有区间
- 是否有人均和总价
- 是否超预算预警
- 是否说明估算来源和假设

### RAG 指标

- Recall@k
- citation coverage
- 来源有效期覆盖率
- 过期信息提示率
- 幻觉率

### 用户体验指标

- 回答是否清晰
- 是否过度追问
- 是否能给选择题
- 是否解释推荐原因
- 是否诚实表达不确定性

## 4. Grader 怎么做

优先使用规则评分，LLM Judge 只做主观补充。

当前项目已经有基础 grader：
- `evals/graders.py:25`
- `evals/graders.py:101`

建议扩展成：

```text
rule-based graders:
  - requirement_completeness
  - step_transition_score
  - tool_selection_score
  - itinerary_executability
  - budget_constraint_score
  - source_coverage_score
  - approval_safety_score

LLM judge:
  - 推荐是否自然
  - 解释是否有帮助
  - 行程体验是否合理
```

安全类、金额类、审批类不要交给 LLM 判断，要用确定性规则。

## 5. 每次评测要保存什么

每次 eval run 保存 artifact：

```json
{
  "case_id": "family_xian_001",
  "input": "...",
  "final_answer": "...",
  "final_state": {},
  "tool_calls": [],
  "rag_contexts": [],
  "sse_events": [],
  "latency_ms": 12000,
  "scores": {},
  "failures": []
}
```

这样失败时可以快速判断问题在：

```text
需求理解
状态流转
工具选择
RAG 检索
行程规划
预算估算
最终表达
```

## 6. CI 准入建议

每次改 Agent / prompt / 工具后跑：

```bash
uv run pytest -q tests/test_planner tests/test_evals
```

再跑离线 eval：

```bash
uv run python -m evals.run --dataset evals/datasets/travel_tasks.jsonl
```

建议设门槛：

```text
overall >= 0.80
requirement_completeness >= 0.90
tool_selection >= 0.85
approval_safety = 1.00
source_coverage >= 0.70
```

## 7. 最重要的一点

这个项目的评测重点不是“回答好不好听”，而是：

```text
状态是否正确
工具是否用对
行程是否可执行
预算是否可信
来源是否可追溯
高风险动作是否被拦截
```

也就是把 Agent 当成一个可测试的软件系统，而不是只靠人工看最终回复。