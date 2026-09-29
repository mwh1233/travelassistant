# Evaluation Datasets

当前评测集合共 250 条样例：240 条内容样例（Agent 主流程、RAG 问答、MCP 工具调用）
加上 10 条轨迹场景。全部采用 **v0.2.0 统一 schema**，由
`scripts/migrate_eval_datasets.py` 生成并可重复重建。

## Files

| File | Count | Purpose |
| --- | ---: | --- |
| `travel_tasks.jsonl` | 120 | 端到端旅行规划、状态更新、多轮修改、兜底和安全风险 |
| `rag_qa.jsonl` | 60 | RAG 检索、引用覆盖、时效判断、低覆盖兜底 |
| `mcp_tool_tasks.jsonl` | 60 | MCP 工具选择、参数抽取、禁止工具、fallback 和风险控制 |
| `trajectory_scenarios.jsonl` | 10 | 轨迹级场景：初始状态 + 脚本 + 期望断言 |

## 公共字段（四个文件都有）

| 字段 | 说明 |
| --- | --- |
| `id` | 稳定 ID，全局唯一，用于回归报告与失败追踪 |
| `type` | 行类别（见下） |
| `split` | `smoke` / `regression` / `challenge` / `safety` / `holdout` |
| `version` | 数据集版本，当前 `0.2.0` |
| `input` | 用户这一轮的自然语言输入 |
| `initial_state` | **必需**。起始 `current_step` 与该步的前置状态 |
| `metadata` | `difficulty` / `requires_realtime` / `multi_turn` / `risk_level` / `tags` |

### 为什么 `initial_state` 是必需的

本系统 **一轮只推进一步**（`add_edge(step, END)` + START 条件路由）。要测"交通规划"，
用例必须预先构造好 `current_step="transport_planning"` 且 `user_requirement`、
`selected_destination` 已填。没有起始状态，用例根本无法定位到某一步。

中游状态由 `step_config[*].requires` 推导，因此**天然满足该步的前置门禁**：

```jsonc
"initial_state": {
  "current_step": "transport_planning",     // 来自 requires
  "user_requirement": { /* ... */ },
  "selected_destination": "西安"
}
```

唯一例外是 `traj_unknown_step_falls_back_010`：它**故意**从非法 step 出发测路由回落，
在 `metadata.deliberately_illegal_initial_step` 里显式登记。断言会双向校验——标了标记
就必须真的是非法 step，反之亦然。

### 工具词表

数据集里出现的每个工具/能力取值都必须落在 `app/mcp_core/registry.py` 的词表内
（由 `L0.capability_vocabulary` 作为硬门禁守住）。词表分三类：

| 类别 | 形态 | 例子 |
| --- | --- | --- |
| MCP 能力 | `<domain>.<action>` | `weather.realtime`、`map.poi`、`map.route` |
| 内部状态工具 | `internal.*` | `internal.transport.select`、`internal.step.rollback` |
| 禁止能力（deny-list） | `<domain>.<action>` | `payment.execute`、`memory.write_sensitive` |

`resolve_tool_id(tool_name)` 把**真实工具名**解析成词表 ID，轨迹断言靠它把
"实际调用了什么"与"期望调用什么"放到同一套词汇上比较。

> `food.search` 已在词表中声明，但当前**没有 MCP server 提供对应工具**。
> 期望它的那两行应视为 fallback 场景，直到接上真实 server。

## Composition

`travel_tasks.jsonl`（按 `type`）:

- `agent_planning`: 92
- `multi_turn_revision`: 12
- `fallback`: 8
- `safety_or_risk`: 8

`rag_qa.jsonl`: `rag_qa` 60 条

`mcp_tool_tasks.jsonl`: `mcp_tool_use` 60 条

### split 分布

| 文件 | smoke | regression | challenge | safety |
| --- | ---: | ---: | ---: | ---: |
| `travel_tasks.jsonl` | 5 | 101 | 7 | 7 |
| `mcp_tool_tasks.jsonl` | 4 | 38 | 3 | 15 |
| `rag_qa.jsonl` | 3 | 54 | 3 | 0 |
| `trajectory_scenarios.jsonl` | 0 | 6 | 2 | 2 |

- `smoke` —— 每类各取一条代表，PR 上先跑这一小撮
- `regression` —— 必须通过
- `challenge` —— 已知困难（工具返回空、来源冲突），失败应被当作信号而非回归
- `safety` —— 有 `forbidden_capabilities`，必须零违规

## 各文件专属字段

`travel_tasks.jsonl`

- `must_collect`：必须收集到的需求字段
- `expected_tools`：期望调用的工具集合（统一词表 ID，含 `internal.*`）
- `forbidden_capabilities`：deny-list，仅 safety 行非空
- `constraints`：约束标签

`mcp_tool_tasks.jsonl`

- `expected_capabilities`：`[{"capability": ..., "required": bool}]`
- `forbidden_capabilities`：禁止调用的能力
- `required_args`：必须传对的参数
- `fallback_expected` / `risk_level`

`rag_qa.jsonl`

- `expected_entities` / `expected_categories`
- `requires_sources` / `freshness_required` / `fallback_expected`

## `trajectory_scenarios.jsonl`

内容样例只描述自然语言输入。轨迹场景则显式给出起始状态与期望：

```jsonc
{
  "id": "traj_transport_param_fidelity_001",
  "type": "trajectory",
  "split": "regression",
  "version": "0.2.0",
  "description": "这条场景在验什么",
  "input": "用户这一轮说的话",

  "initial_state": {               // 必需：决定路由到哪个节点
    "current_step": "transport_planning",
    "user_requirement": { /* ... */ },
    "selected_destination": "西安"
  },

  "script": [                      // deterministic 模式下模型被脚本化重放的动作
    {"tool": "query_transport_options", "args": {"origin_city": "北京"}}
  ],

  "expect": {
    "routed_node": "transport_planning",
    "tools_called": ["query_transport_options"],
    "tools_not_called": ["go_back_to_requirement"],
    "arg_fidelity": [              // 参数保真：工具参数必须等于某个 state 路径
      {"tool": "query_transport_options",
       "args": {"origin_city": "user_requirement.departure_city"}}
    ],
    "final_step": "transport_planning",
    "state_null": ["structured_itinerary"],       // 回退后必须被清空
    "state_not_null": ["selected_destination"],   // 必须保留
    "state_equals": {"selected_transport": "train"},
    "llm_calls_max": 0
  },

  // 特殊标记
  "known_gap": true,               // 预期失败且已归入已知缺口，不计入回归
  "known_gap_ref": "docs/agent-eval-design.md P0-5",
  "negative_control": true,        // 负向对照：断言必须失败才算通过
  "expect_failures": ["L2.arg_fidelity"]
}
```

除场景声明的期望外，每个场景都会无条件执行一组不变量断言（见
`evals/trajectory_checks.py`）：执行无异常、无重复 `(tool, args)` 调用、
一轮最多前进一步、工具选择在候选集内、高风险动作前置审批、轨迹参数与实参一致。

## 重建数据集

```bash
python -m scripts.migrate_eval_datasets --check   # 干跑，只报告
python -m scripts.migrate_eval_datasets --write   # 重写 JSONL
```

脚本从 `step_config` 读取 `requires`、从 `registry` 读取词表，因此词表或步骤契约
变化后重跑即可同步。校验不通过时**不会写入**。
