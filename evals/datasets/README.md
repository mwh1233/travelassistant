# Evaluation Datasets

当前评测集合共 120 条 JSONL 样例，覆盖 Agent 主流程、RAG 问答和 MCP 工具调用。

## Files

| File | Count | Purpose |
| --- | ---: | --- |
| `travel_tasks.jsonl` | 60 | 端到端旅行规划、状态更新、多轮修改、兜底和安全风险 |
| `rag_qa.jsonl` | 30 | RAG 检索、引用覆盖、时效判断、低覆盖兜底 |
| `mcp_tool_tasks.jsonl` | 30 | MCP 工具选择、参数抽取、禁止工具、fallback 和风险控制 |

## Composition

`travel_tasks.jsonl`:

- `agent_planning`: 46
- `multi_turn_revision`: 6
- `fallback`: 4
- `safety_or_risk`: 4

`rag_qa.jsonl`:

- `rag_qa`: 30

`mcp_tool_tasks.jsonl`:

- `mcp_tool_use`: 30

## Notes

- 每条样例都有稳定 `id`，用于回归报告和失败样例追踪。
- Agent 主任务保留 `input`、`must_collect`、`expected_tools`、`constraints`，兼容当前轻量 grader。
- RAG 专项样例使用 `expected_entities`、`expected_categories`、`requires_sources`、`freshness_required`、`fallback_expected`。
- MCP 专项样例使用 `expected_capabilities`、`forbidden_capabilities`、`required_args`、`fallback_expected`、`risk_level`。
