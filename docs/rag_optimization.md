# RAG 优化方案

> 本文针对当前旅行助手项目的 RAG 实现，整理现有链路、主要问题、优化方向、实验方法和落地优先级。本文只描述方案，不代表所有建议已经实现。

## 1. 优化目标

旅行场景的 RAG 不能只追求语义相似度，还需要保证：

- 召回的内容确实与用户问题相关。
- 内容适用于指定城市、景点、人群和旅行条件。
- 门票、开放时间、预约、交通和政策等信息足够新。
- 最终回答可以被来源支持。
- 证据不足时能够降级，而不是编造答案。
- RAG 结果可以被 Agent 直接用于行程、预算和风险判断。

核心目标可以概括为：

```text
高召回
  + 高精度
  + 高时效
  + 高可信来源
  + 可解释引用
  + 可控成本
```

## 2. 当前 RAG 链路

当前项目已经具备较完整的基础链路：

```text
用户查询
  -> Query Optimizer
  -> Multi-Query 查询变体
  -> BM25 + Dense 混合检索
  -> RRF 融合
  -> LLM Reranker
  -> Parent Document 映射
  -> Long Context Reorder
  -> 缓存
  -> 返回带来源上下文
```

主要实现位置：

| 能力 | 文件 |
| --- | --- |
| RAG 总管道 | `app/rag/pipeline.py` |
| 混合检索 | `app/rag/retriever.py` |
| 查询优化 | `app/rag/query_optimizer.py` |
| LLM 重排 | `app/rag/reranker.py` |
| 父子文档切分 | `app/rag/text_splitter.py` |
| Chroma 向量库 | `app/rag/vectorstore.py` |
| 来源和时效字段 | `app/rag/document_loader.py`、`app/rag/pipeline.py` |
| RAG 数据集 | `evals/datasets/rag_qa.jsonl` |

当前 RAG 使用的关键能力包括：

- Multi-Query 查询改写。
- BM25 与向量检索结合。
- RRF 倒数排名融合。
- LLM 相关性重排。
- 父文档和子文档映射。
- 长上下文顺序优化。
- 结果缓存。
- `source`、`source_url`、`updated_at`、`valid_until` 等来源 metadata。

## 3. 当前主要问题

### 3.1 中文关键词检索需要验证

项目存在基于 `jieba` 的 BM25 逻辑，同时当前高级检索器使用 `BM25Retriever`。需要确认当前实际运行路径是否使用中文分词。

旅行查询中有大量专有词：

```text
陕西历史博物馆
鼓浪屿船票
老人少走路
高铁直达
清真餐饮
宠物友好酒店
```

如果分词不稳定，BM25 对景点名、政策词和约束词的召回会明显下降。

优化方向：

- 建立城市、景点、酒店、餐厅和政策领域词典。
- 对中文、英文、数字、日期分别规范化。
- 对实体名使用完整词和别名同时检索。
- 对中文分词 BM25、字符 n-gram 和普通 BM25 做离线对比。

### 3.2 Metadata 还没有充分参与检索

当前文档已经有来源、类别和时间字段，但主要链路仍然依赖文本相似度。旅行场景需要将 metadata 变成检索约束。

建议使用的字段：

```text
city
district
category
audience
travel_style
season
requires_reservation
source_type
source_authority
updated_at
valid_until
```

例如：

```text
西安适合老人、少走路的历史景点
```

应优先筛选：

```text
city = 西安
audience 包含 elderly
category = attraction
low_walking = true
```

再进行 BM25 和向量检索，而不是只依赖 Embedding 理解这些约束。

### 3.3 时效字段还需要参与排序和回答策略

文档有 `updated_at` 和 `valid_until` 并不等于真正实现了时效控制。如果过期内容仍能排到前面，最终回答依然可能不可靠。

建议把信息分为：

| 等级 | 示例 | 推荐策略 |
| --- | --- | --- |
| T0 | 历史背景、地理介绍 | 普通 RAG |
| T1 | 景点特点、路线建议 | 定期更新 |
| T2 | 门票、开放时间、预约 | 强制检查更新时间 |
| T3 | 天气、交通、政策、价格 | 优先实时工具 |
| T4 | 付款、库存、订单 | 不依赖普通 RAG 直接执行 |

实时问题应采用：

```text
实时工具查询
  -> RAG 提供背景知识
  -> 合并来源和更新时间
  -> 输出不确定性说明
```

### 3.4 Multi-Query 不适合所有查询

Multi-Query 可以提高召回，但会增加 LLM 调用、延迟和噪声。以下问题通常不需要多个查询：

```text
西安城墙门票多少钱？
陕西历史博物馆需要预约吗？
云水间民宿在哪里？
```

建议按查询类型路由：

| 查询类型 | 策略 |
| --- | --- |
| 明确实体查询 | 原始查询 + 关键词检索 |
| 口语化查询 | Query Rewrite |
| 多条件规划 | Query Decomposition |
| 表达差异较大的主题查询 | Multi-Query |
| 长文解释型问题 | 可实验 HyDE |
| 实时事实查询 | 先调用实时工具 |

### 3.5 LLM Reranker 成本和稳定性需要控制

当前重排器使用 LLM 对候选文档评分。需要关注：

- 每次检索增加一次模型调用。
- 只查看文档摘要时可能忽略关键字段。
- 可能漏返回、重复返回或返回越界文档索引。
- 只判断相关性，不一定判断来源可信度和时效性。
- 查询变体过多时，候选文档噪声会增加。

建议采用两阶段重排：

```text
BM25 + Dense
  -> 召回 20-50 个候选
  -> 规则或轻量模型过滤
  -> LLM 只重排前 5-10 个
  -> 返回 3-5 个上下文
```

## 4. 检索层优化方案

### 4.1 混合检索参数实验

不要固定认为 Dense 权重一定高于 BM25。建议对以下组合做离线实验：

| 实验 | BM25 | Dense | 融合方式 |
| --- | ---: | ---: | --- |
| A | 0 | 1.0 | Dense |
| B | 1.0 | 0 | BM25 |
| C | 0.3 | 0.7 | RRF |
| D | 0.5 | 0.5 | RRF |
| E | 0.7 | 0.3 | RRF |
| F | 动态 | 动态 | 按查询类型路由 |

同时测试：

- 候选数量 `k`。
- 最终上下文数量。
- RRF 的常数参数。
- 查询变体数量。
- 是否启用 LLM 重排。

### 4.2 Query Decomposition

复杂旅行问题可以先拆成多个子查询：

```text
原问题：
带父母去苏州三天，想看园林，不要走太多路，住地铁附近，预算每人 2000。

子查询：
1. 苏州适合老人的园林
2. 苏州园林步行强度
3. 苏州地铁附近住宿区域
4. 苏州三天低强度路线
5. 苏州人均 2000 的预算范围
```

适合使用 Query Decomposition 的场景：

- 多条件旅行规划。
- 多城市路线。
- 亲子或老人出行。
- 预算、交通、景点组合问题。
- 多轮修改后的局部重算。

### 4.3 动态查询路由

可以先判断查询类型，再选择 RAG 策略：

```text
实体查询 -> 实体精确检索
事实查询 -> BM25 + Dense
复杂规划 -> 子查询拆分
实时问题 -> MCP/实时 API
低覆盖问题 -> 搜索兜底 + 不确定性回答
```

这样可以减少所有请求都经过 Multi-Query 和 LLM Reranker 带来的成本。

## 5. 文档和知识库优化

### 5.1 按文档类型切分

固定 chunk size 可以作为默认策略，但不适合所有旅游资料。

| 文档类型 | 推荐切分方式 |
| --- | --- |
| 景点介绍 | 景点实体级 |
| 政策公告 | 规则条款级 |
| 门票和预约 | 字段或表格级 |
| 酒店信息 | 酒店实体级 |
| 路线攻略 | 路线步骤级 |
| FAQ | 问题和答案成组 |
| 长游记 | 标题和段落级 |

不要把一个事实拆散到多个互不相关的 chunk：

```text
景点名称
开放时间
门票价格
预约规则
交通方式
```

这些字段通常应该保留在同一个实体上下文内。

### 5.2 为 chunk 补充层级上下文

每个 chunk 建议包含完整标题和实体信息：

```text
城市：西安
分类：景点
景点：陕西历史博物馆
主题：预约规则
正文：需要提前预约……
```

这样可以增强关键词检索、向量检索、重排和最终引用。

### 5.3 建立实体别名

旅行数据中存在简称、旧称、英文名和口语名：

```text
故宫 -> 故宫博物院
大熊猫基地 -> 成都大熊猫繁育研究基地
鼓浪屿船票 -> 厦门轮渡鼓浪屿航线
```

建议在文档 metadata 或实体索引中记录：

```text
canonical_name
aliases
city
entity_type
```

## 6. 来源可信度和冲突处理

建议建立来源等级：

```text
官方公告 / 官方平台
  > 官方机构页面
  > 权威媒体
  > 专业旅行平台
  > 用户评论
  > 普通博客
  > 社交媒体
```

当多个来源冲突时，优先级可以是：

1. 官方来源优先。
2. 更新时间更新者优先。
3. 与用户实体和日期匹配度更高者优先。
4. 内容更具体者优先。
5. 无法解决时明确说明冲突。

可以定义一个来源质量分：

```text
source_score =
  authority_score
  + freshness_score
  + entity_match_score
  + completeness_score
```

必须避免：

- 把搜索摘要当作完整事实。
- 把用户评论当作官方政策。
- 使用过期攻略覆盖新公告。
- 把同名景点、餐厅或酒店混淆。
- 推荐已经停业的商家。

## 7. RAG 评测优化

当前 `rag_qa.jsonl` 已覆盖实体、分类、来源、时效和 fallback。后续建议增加 gold context 和答案约束。

推荐样例字段：

```json
{
  "id": "rag_xian_museum_001",
  "type": "rag_qa",
  "input": "陕西历史博物馆需要预约吗？",
  "expected_entities": ["陕西历史博物馆"],
  "expected_categories": ["reservation"],
  "gold_context_ids": ["doc_001", "doc_017"],
  "must_include": ["预约规则", "官方来源"],
  "must_not_include": ["无来源的确定性价格"],
  "requires_sources": true,
  "freshness_required": true,
  "fallback_expected": false
}
```

评测要分层执行：

```text
Context Recall
  -> Context Precision
  -> Reranking Quality
  -> Citation Coverage
  -> Citation Correctness
  -> Faithfulness
  -> Answer Relevancy
```

这样可以判断问题属于：

- 没召回正确资料。
- 召回了但排序错误。
- 证据存在但回答没有使用。
- 回答超出了证据范围。

### 7.1 推荐指标

| 指标 | 作用 |
| --- | --- |
| Recall@K | 正确证据是否进入前 K 个结果 |
| MRR | 第一个正确证据出现得是否足够靠前 |
| nDCG@K | 多个相关文档的排序质量 |
| Context Precision | 上下文中有效信息的比例 |
| Citation Coverage | 关键事实的引用覆盖率 |
| Citation Correctness | 引用是否支持对应事实 |
| Faithfulness | 回答是否被上下文支持 |
| Answer Relevancy | 回答是否真正解决问题 |
| Freshness Pass Rate | 时效性要求是否满足 |
| Fallback Accuracy | 低覆盖问题是否正确降级 |

## 8. 缓存和成本优化

### 8.1 缓存策略

缓存不仅要以 query 为 key，还应考虑：

```text
normalized_query
embedding_version
retriever_version
document_version
metadata_filter
top_k
```

否则文档更新、Embedding 更新或过滤条件改变后，可能返回旧结果。

### 8.2 查询分级

可以按问题复杂度分级：

| 等级 | 处理方式 |
| --- | --- |
| 简单 | 单次检索，关闭额外重排 |
| 普通 | BM25 + Dense + RRF |
| 复杂 | Query Decomposition + 混合检索 |
| 实时 | MCP/实时 API + RAG 背景资料 |
| 低覆盖 | 搜索兜底 + 明确不确定性 |

### 8.3 LLM 调用预算

对于每次请求建议记录：

```text
query_rewrite_calls
reranker_calls
retrieval_latency_ms
reranker_latency_ms
input_tokens
output_tokens
estimated_cost
cache_hit
```

最终可以比较：

```text
quality_gain / additional_cost
```

如果某个优化只增加成本但没有显著提高 Faithfulness 或 Recall，就不应该默认启用。

## 9. 推荐实验计划

### Phase 1：建立基线

固定以下变量：

- 数据集版本。
- Embedding 模型。
- 当前日期。
- `top_k`。
- Query Strategy。
- RRF 参数。
- Reranker 开关。

记录：

```text
Recall@3
Recall@5
MRR
nDCG@5
Citation Coverage
Faithfulness
Answer Relevancy
P95 latency
Token cost
```

### Phase 2：单变量实验

按照以下顺序逐个实验：

1. 中文 tokenizer 和领域词典。
2. BM25/Dense 权重。
3. 候选数量和最终 `top_k`。
4. Query Rewrite、Multi-Query 和 Query Decomposition。
5. 是否启用 HyDE。
6. LLM Reranker。
7. 父文档和子文档大小。
8. Metadata filtering。
9. Freshness ranking。
10. Source authority ranking。

每次只改变一个变量，避免无法判断收益来源。

### Phase 3：按场景分层比较

不能只看总平均分，需要分别统计：

```text
实体查询
多条件查询
实时查询
低覆盖查询
来源冲突查询
亲子查询
老人查询
预算查询
政策查询
```

一种策略可能提升普通问答，却降低实时问题或低覆盖问题的安全性。

## 10. 实施优先级

### P0：先验证基础正确性

- 确认当前实际使用的 BM25 分词方式。
- 检查文档 metadata 完整性和字段一致性。
- 检查缓存是否考虑文档版本和检索参数。
- 为 RAG 样例增加 gold context 或人工相关文档标注。

### P1：投入产出比最高

- 增加中文领域词典。
- 增加城市、类别、人群和时效过滤。
- 增加来源权威排序。
- 增加实时问题路由到 MCP。
- 将 RAG 评测拆分为召回、排序、引用和生成四层。

### P2：质量和成本优化

- 复杂问题采用 Query Decomposition。
- 简单查询关闭 Multi-Query 或 LLM Reranker。
- 对候选文档先做轻量过滤，再做 LLM 重排。
- 根据查询类型动态调整 BM25/Dense 权重。

### P3：长期建设

- 建立旅行实体索引或轻量知识图谱。
- 增加来源冲突检测。
- 建立线上失败 trace 到离线数据集的回流流程。
- 维护文档有效期、版本和来源质量。

## 11. 面试总结话术

当前项目已经有 Multi-Query、BM25/Dense 混合检索、RRF、LLM 重排和父子文档能力。下一步不会简单堆更多 RAG 组件，而是做旅行领域化优化：用城市、景点、人群、预约和有效期 metadata 做过滤与排序；用 Query Decomposition 处理多条件旅行问题；把天气、交通、价格和政策等实时信息路由到实时工具；最后通过 Recall、MRR、Citation Coverage、Faithfulness 和 Answer Relevancy 分层评测，判断问题究竟出在召回、排序、证据使用还是最终生成。
