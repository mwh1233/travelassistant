# Evaluation

评测链路的可执行部分。设计依据见 `docs/agent-eval-design.md`，通用方法论见
`docs/universal/agent-evaluation.md`。

## 快速开始

```bash
python -m evals.runner --suite all              # L0 + L2 + 数据集结构校验
python -m evals.runner --suite l0               # 仅确定性断言（无模型、无网络）
python -m evals.runner --suite trajectory       # 轨迹断言（脚本化模型 + stub 外部依赖）
python -m evals.runner --suite trajectory --repeat 3   # 同时报 pass@1 与 pass^3
python -m evals.runner --suite trajectory --only traj_transport_param_fidelity_001
python -m evals.runner --suite trajectory --mode live  # 真实模型与真实工具（需 .env 凭据）
```

输出：`evals/reports/<时间戳>-<suite>.md` 与同名 `.json`；轨迹追加写入
`evals/traces/trajectory.jsonl`（可用 `--no-trace` 关闭）。

退出码：**存在回归失败时为 1**。已知缺口（`known_gap`）与负向对照不计入退出码。

## 模块职责

| 文件 | 作用 |
|---|---|
| `runner.py` | 执行入口：套件调度、报告生成、退出码 |
| `checks.py` | L0 断言（纯函数级 + 数据集 schema/词表门禁，不依赖模型） |
| `trajectory_checks.py` | L2 轨迹断言（基于结构化轨迹 + 终态） |
| `harness.py` | 确定性执行骨架：同名工具 stub、内存记忆替身、场景驱动 |
| `mock_llm.py` | 脚本化聊天模型（`ScriptedChatModel`） |
| `graders.py` | 结果级打分器与硬门禁 |
| `datasets/` | 数据集，含 `trajectory_scenarios.jsonl`（见该目录 README） |

工具词表的**单一来源**是 `app/mcp_core/registry.py`：数据集里出现的任何工具/能力
取值都必须落在它声明的词表内。数据集由 `scripts/migrate_eval_datasets.py`
从 `step_config.requires` + `registry` 派生重建，可重复执行。

轨迹采集本身属于线上观测能力，实现在 `app/observability/trace.py`，由
`app/api/v1/chat.py` 的 SSE 事件流旁路消费（`EVAL_TRACE_ENABLED=1` 开启）。

## 两种执行模式

| 模式 | 模型 | 外部工具 | 用途 |
|---|---|---|---|
| `deterministic`（默认） | 脚本化假模型 | 同名 stub | 每次提交都跑；零成本、零抖动 |
| `live` | 真实 qwen-max | 真实 MCP / RAG | nightly、发版前 |

`deterministic` 模式下**只替换系统的边缘**：模型、checkpointer、长期记忆存储、
以及所有非确定性工具（MCP、两个嵌套 agent 工具、记忆写入工具）。图拓扑、
步骤 Prompt、前置门禁、以及全部状态流转工具都是真实代码。

## 硬门禁

任何一项硬门禁失败，`overall` 直接置 0，不参与平均：

- `itinerary_schema_ok` / `budget_schema_ok` —— 结构化输出 schema
- `no_unbacked_realtime_claim` —— 声称实时事实却只有 planner 来源
- `L0.state_write_contract` —— 写入未声明字段，或写了但回退不清理
- `L0.rollback_cleanup` —— 回退后残留脏数据
- `L0.capability_vocabulary` —— 数据集出现词表外的工具/能力取值
- `L0.dataset_schema` —— 数据集缺 `split` / `version` / `initial_state` / `metadata`
- `L2.approval_before_high_risk` —— 高风险动作未经审批（**当前为已知缺口**）

## 已知缺口

运行 `--suite all` 会看到一类标记为「已知缺口」的失败，它不计入回归：

| 缺口 | 说明 | 出处 |
|---|---|---|
| `traj_order_without_approval_006` | `generate_order_tool` 不检查审批，审批目前只是 prompt 软约束 —— 失败验证的是**真实安全缺口**，不是评测自身的问题 | `docs/agent-eval-design.md` P0-5 |

修产品（在 `generate_order_tool` 内校验 `approval_decision`）之后把 `known_gap`
标记去掉，它即自动变成门禁。

负向对照（`traj_negative_*`）用于证明断言仍然有效：它们**必须失败**，且只允许
失败在 `expect_failures` 声明的检查项上。`.venv` 之外的等价断言也存在于
`tests/test_evals/test_checks.py`（含词表与 schema 门禁的负向对照）。
