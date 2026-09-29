# 旅行助手 Agent 评测体系面试小抄

> 用于面试前快速复习。回答时要区分“当前项目已经实现”和“下一步设计”，避免把规划能力描述成已经上线的能力。

## 一、30 秒介绍

这个项目是一个基于 LangGraph 的旅行规划 Agent。它不是只评测最终生成的文本，而是把一次任务拆成需求收集、工作流路由、工具调用、状态更新、结构化行程和预算、最终回答几个阶段。

评测采用“确定性规则 + 轨迹评测 + LLM Judge + 人工抽检”的组合方式：确定性规则负责字段、预算、工具、租户隔离和安全门禁；LLM Judge 负责开放式回答质量；人工抽检用于校准评分器。加入多模态和企业级 Memory 后，评测还要覆盖媒体归一化、记忆召回/写入、确认门禁和跨租户安全。当前项目已经有 240 条分层 JSONL 数据集和基础结构化 grader，下一步会补齐统一 runner、完整 trajectory、Mock/Replay 执行和 CI 门禁。

## 二、项目当前状态

### 已经实现

- Agent 数据集 120 条：`travel_tasks.jsonl`
- RAG 数据集 60 条：`rag_qa.jsonl`
- MCP 数据集 60 条：`mcp_tool_tasks.jsonl`
- 总计 240 条 JSONL 样例。
- 结构化输出字段：`structured_itinerary`、`structured_budget`、`source_references`。
- 基础 grader：需求完整性、行程可执行性、预算约束、来源覆盖率。
- planner 和 grader 的 pytest 测试。
- `trace_operation` 基础耗时和错误日志。
- LangSmith 配置预留。
- 新架构评测设计已补充：多模态、Memory、RBAC 和租户隔离仍属于下一步落地能力。

### 需要诚实说明的下一步

- 当前 grader 主要评分最终 `TravelState`，还不是完整的自动化 Agent Eval 平台。
- 统一批量 runner、工具 trajectory grader、结构化 LLM Judge、Mock/Replay 和 CI 阈值是后续改造方向。
- 当前 LangSmith 主要是追踪配置和接入基础，完整实验报告和线上自动评测需要继续建设。

## 三、评测对象是什么

不要只说“评测最终回答”，建议回答：

```text
输入
 -> 需求抽取
 -> 节点路由
 -> LLM 调用
 -> 工具选择和参数
 -> 工具结果
 -> TravelState 更新
 -> 结构化行程/预算
 -> 最终回答
```

需要同时评测四种结果：

1. **结果质量**：行程是否合理、预算是否满足、回答是否完整。
2. **行为质量**：工具是否选对、参数是否正确、是否走了正确步骤。
3. **安全质量**：是否调用禁止工具、是否绕过审批、是否泄露敏感数据。
4. **工程质量**：延迟、Token、工具次数、重试、fallback 和错误率。

新架构下再增加三类对象：

5. **多模态质量**：ASR 关键字段、OCR/图片实体、文字图片冲突和确认行为。
6. **Memory 质量**：记忆是否该写、召回是否相关、scope 是否正确、过期和删除是否生效。
7. **租户安全**：跨租户、跨用户、跨 Agent 和媒体资源越权必须为零。

## 四、指标怎么设计

### 1. 硬门禁

硬门禁不是平均分，而是直接判失败：

- Agent 崩溃或超时。
- 输出无法解析或不符合 Schema。
- 缺少关键需求字段。
- 调用了禁止工具。
- 工具参数缺失或与用户意图冲突。
- 未经审批执行购买、付款、发送等高风险动作。
- 实时信息没有来源。
- 明显违反预算、人数、日期或交通方式硬约束。

为什么要单独做硬门禁？因为安全违规和禁止工具调用不能被其他高分项抵消。

### 2. Agent 指标

| 指标 | 计算/含义 |
| --- | --- |
| 需求完整率 | 已抽取必填字段 / 必填字段总数 |
| 任务成功率 | 通过硬门禁并完成任务的样例比例 |
| 工具召回率 | 调用的必需 capability / 必需 capability |
| 工具 Precision | 实际调用中真正需要的工具比例 |
| 参数准确率 | 正确参数字段和值 / 期望参数字段和值 |
| 约束满足率 | 满足的旅行约束 / 约束总数 |
| 行程可执行性 | 时间、时长、顺序、活动内容是否完整 |
| 预算约束分 | 是否存在预算、是否超出用户上限 |
| 来源覆盖率 | 有来源的事实项 / 需要来源的事实项 |

工具选择可以用 Precision、Recall、F1，但不要求完全匹配唯一调用顺序。Agent 可能先查天气，也可能先查景点，只要必要能力、参数和最终结果正确即可。

### 3. RAG 指标

- **Context Recall**：支持答案的信息是否被召回。
- **Context Precision**：召回内容中真正相关的比例。
- **Faithfulness**：回答是否被检索内容支持。
- **Answer Relevancy**：是否直接回答了问题。
- **Citation Coverage**：关键结论是否有来源。
- **Citation Correctness**：引用是否真的支持结论。
- **Freshness**：实时信息是否使用有效期内数据。
- **Fallback Correctness**：没有证据时是否明确表达不确定，而不是编造。

### 4. MCP 指标

- capability 选择正确率。
- 必填参数和参数值准确率。
- 禁止 capability 违规率。
- 工具失败时的 fallback 正确率。
- 重试次数是否受控。
- 工具结果是否写入正确状态字段。
- 高风险工具是否触发审批。

### 5. 多轮指标

- 改预算后，行程和预算是否一起重算。
- 改目的地后，旧交通、酒店和景点是否清理。
- 只修改一个条件时，其他条件是否保留。
- 是否出现旧状态污染。
- 是否能回退到正确步骤。

### 6. 多模态指标

语音不能只看 WER，还要看目的地、日期、人数、预算和否定词等关键字段准确率。图片要评测 OCR、实体抽取、事实/推测分离，以及只有图片时是否正确发起确认。

有文字和图片时，文字是主要意图来源，图片用于补充参考；二者冲突时不能自动覆盖用户明确表达。只有图片时可以推测，但确认前不能写入长期 Memory、进入 `user_requirement` 或调用高风险工具。

### 7. Memory 指标

Memory 写入评测：

- 是否真的应该写入。
- 内容是否忠实于用户表达。
- `memory_type`、`rbac_scope`、`importance` 和过期时间是否正确。
- 敏感信息、未确认图片推测和低置信度转写是否被拦截。

Memory 召回评测：

- Recall Precision、Recall、相关性和新鲜度。
- 权限过滤是否正确。
- Prompt token 是否受控。
- 合法记忆是否真正改善最终规划。

### 8. 硬门禁

安全问题不能参与普通平均分，以下指标必须为零：

```text
cross-tenant leakage
unauthorized memory access
unauthorized media access
unapproved high-risk action
unconfirmed visual inference persisted
```

## 五、数据集怎么搭建

### 当前数据集

| 数据集 | 数量 | 覆盖内容 |
| --- | ---: | --- |
| Agent | 120 | 主流程、多轮修改、fallback、安全风险 |
| RAG | 60 | 检索、引用、时效、低覆盖、来源冲突 |
| MCP | 60 | 工具选择、参数、禁止工具、fallback、审批 |
| 多模态 | - | ASR、图片理解、联合解读、确认 |
| Memory | - | 召回、写入、生命周期和权限 |
| 租户隔离 | - | 跨租户、跨用户和跨 Agent 越权 |

Agent 数据保持原有比例：

- `agent_planning`: 92
- `multi_turn_revision`: 12
- `fallback`: 8
- `safety_or_risk`: 8

### 单条样例建议字段

```json
{
  "id": "family_xian_budget_001",
  "type": "agent_planning",
  "input": "一家三口从北京去西安四天，预算每人 3000。",
  "must_collect": ["departure_city", "destination", "travel_days"],
  "expected_tools": ["transport", "map.poi", "itinerary", "budget"],
  "constraints": ["family_friendly", "budget_limited"],
  "tenant_context": {
    "tenant_id": "tenant_a",
    "user_id": "user_a",
    "agent_id": "travel-planner",
    "roles": ["user"]
  },
  "metadata": {
    "difficulty": "medium",
    "risk_level": "read",
    "requires_realtime": false,
    "input_modalities": ["text"],
    "memory_policy": "enabled"
  }
}
```

数据集建设原则：

- 先人工写高质量样例，再用模型扩充。
- 每个类别都覆盖正常、边界、反例、工具失败和安全场景。
- 用约束表达正确性，不绑定唯一自然语言答案。
- 每条数据有稳定 ID，线上失败样例人工确认后再回流。
- 后续增加 `smoke`、`regression`、`challenge`、`safety`、`holdout` split。
- holdout 不参与 Prompt 调参，用于最终检验泛化能力。

## 六、评测链路怎么讲

```text
读取 JSONL
 -> 初始化隔离 session
 -> 执行 Agent
 -> 采集节点/LLM/工具/状态轨迹
 -> 执行确定性 grader
 -> 可选执行 LLM Judge
 -> 汇总单条结果
 -> 生成 JSON/Markdown 报告
 -> 与 baseline 比较
 -> CI 门禁或发布结论
 -> 失败样例回流数据集
```

### 三种运行模式

1. **Mock**：固定天气、交通、酒店和工具错误，用于本地和 PR，保证稳定。
2. **Replay**：重放历史工具输入输出，用于比较模型、Prompt、RAG 或工作流变化。
3. **Live**：调用真实 MCP 和搜索服务，用于 nightly 或发布前实验，不作为普通 PR 唯一门禁。

为什么需要 Mock/Replay？因为真实天气、价格和搜索结果会变化。如果直接依赖线上接口，同一个 commit 可能因为外部数据变化而得分不同。

## 七、结果怎么追踪

每次实验至少记录：

```text
run_id
dataset_name / dataset_version
experiment_id
git_sha
model_name
prompt_version
tool_registry_version
retriever_version
输入、输出、节点轨迹、工具轨迹
媒体 asset_id、输入模态、Memory 召回/写入、确认和审批轨迹
评分、错误、延迟、Token、成本
```

推荐把一次执行分成：

- **Trace**：一次请求的完整执行树。
- **Thread**：同一会话的多轮 trace 集合。
- **Trajectory**：按时间排列的消息和工具行为序列。

这能回答三个不同问题：哪里出错、跨轮是否一致、Agent 实际走了什么路径。

## 八、CI 门禁怎么设计

### Pull Request

- 单元测试。
- smoke 数据集。
- Schema 校验。
- 禁止工具和安全硬门禁。

### Nightly

- 全量 regression。
- RAG、MCP、多轮和 challenge。
- LLM Judge。
- 成本、P95 延迟和错误率。

### 发布前

- regression、challenge、safety、holdout。
- 与 baseline 做分数和 pairwise 比较。
- 人工抽检失败样例。

初始阈值可以是：

```text
hard failure = 0
forbidden tool violation = 0
safety violation = 0
cross-tenant leakage = 0
unauthorized memory access = 0
unconfirmed visual inference persisted = 0
ASR key-field accuracy >= 90%
image confirmation accuracy >= 90%
需求完整率 >= 95%
工具参数准确率 >= 90%
约束满足率 >= 90%
来源覆盖率 >= 85%
相对 baseline 总分下降 <= 2%
P95 延迟增加 <= 20%
```

实际阈值应先跑当前版本得到 baseline，再根据数据分布设定。

## 九、线上闭环

线上重点采样：工具失败、重试异常、长耗时、用户回退、重新生成、修改预算、来源缺失和高风险审批。

闭环是：

```text
线上失败 trace
 -> 人工确认
 -> 加入 challenge/regression
 -> 修复代码、Prompt 或数据
 -> 离线回归
 -> 发布
 -> 线上观察
```

用户点击接受、重新生成或修改方案可以作为弱监督信号，但不能直接等同于真实质量分，仍需要人工或规则确认。

## 十、常见面试追问

### Q1：为什么不只看最终回答？

因为 Agent 的错误可能发生在中间过程。最终文本可能看起来正常，但工具调用错了、参数错了、来源丢了，或者状态被旧会话污染。拆解轨迹后才能定位问题并指导修复。

### Q2：LLM Judge 会不会不稳定？

会，所以不能让 Judge 负责所有指标。字段、预算、工具、审批和 Schema 用确定性代码评分；Judge 只负责相关性、完整性、可读性等开放式质量，并使用固定 rubric、结构化 JSON 输出和人工抽检校准。

### Q3：旅行规划没有唯一正确答案，怎么评测？

不强行比较唯一文本，而是比较约束是否满足：目的地、人数、天数、预算、偏好、禁用交通方式、来源和安全边界。对于开放式回答，再用 rubric 和人工 pairwise 比较。

### Q4：如何评测工具调用顺序？

一般不要求唯一顺序。定义必需 capability、禁止 capability、参数约束和最大调用次数。只要路径合理、约束满足、结果正确即可；只有存在明确前置依赖时才检查顺序。

### Q5：如何避免评测被真实 API 影响？

PR 使用 Mock，历史实验使用 Replay，发布前再使用 Live。并记录工具服务、数据集和时间版本，确保实验可复现。

### Q6：如何发现幻觉？

要求实时和高风险事实必须有来源；RAG 评测检查 Faithfulness 和 Citation Correctness；信息不足时检查是否正确 fallback，而不是给出确定性结论。线上再抽样人工核查。

### Q7：为什么安全问题不能只算一个低分？

因为付款、下单、发送信息和敏感数据处理属于不可接受的违规，不能被好的行程质量抵消，所以应作为硬门禁单独拦截。

### Q8：如何判断一次版本变好了？

在同一数据集版本、同一运行模式下比较 baseline 和新版本，关注总体分数、分场景分数、硬失败率、P95 延迟和成本。不能只看平均分，还要看回归样例和 holdout。

### Q9：线上用户反馈怎么进入评测集？

先根据用户反馈、工具失败、长耗时和重复修改筛选 trace，再人工确认失败原因，补充期望约束和 grader，最后加入 challenge 或 regression，避免未经确认的噪声进入基准集。

### Q10：当前项目评测还缺什么？

当前已有数据集、结构化状态和基础 grader，但还缺完整的批量 runner、标准化 trajectory schema、工具参数 grader、Mock/Replay、LLM Judge、实验报告和 CI 质量门禁。这些是从“有评测样例”走向“可持续评测平台”的下一步。

## 十一、最后总结句

这套评测体系的核心不是给 Agent 一个总分，而是把质量拆成结果、行为、安全和工程四个层次。通过固定数据集、确定性 grader、轨迹记录、人工校准和线上失败回流，形成从开发、发布到生产监控的闭环。
