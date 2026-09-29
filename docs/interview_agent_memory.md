# Agent 后端面试高频 6 问 · 口述答案（结合本项目）

> 项目现状锚点：LangGraph 1.0.5 + Postgres Checkpointer + Postgres Store（结构化长期记忆）+ Chroma（RAG 知识库）+ Redis（RAG 缓存）+ LLM Reranker。
> 原则：做过的讲细节，没做的讲"生产化演进方向"，不吹。

---

## Q1. Agent 三层记忆模型是什么？瞬时 / 会话 / 长期分别用什么存储，为什么这么选型？

### 一句话核心
按"存活时长 + 读写延迟 + 容量"分三层，每层选最匹配的存储，不要把所有历史都塞 Prompt。

### 口述展开
1. **瞬时记忆 = LLM 上下文窗口**
   就是当前 thread 里的 messages 和最近几轮工具结果，活在内存里，模型原生可见。窗口满了就滑动截断 + 长历史摘要压缩。它不是持久化的，进程重启就没了。
   本项目里就是 `TravelState.messages`。

2. **会话记忆（短期）= LangGraph Checkpoint**
   存的是 graph 节点状态、子任务进度、临时变量，按 `thread_id` 隔离，作用是**断点续跑**——比如用户说"等等我改一下目的地"，下次进来能从上个节点接着走。
   **本项目选 PostgreSQL（`AsyncPostgresSaver`）而不是 Redis**，理由：checkpoint 是事务性状态，进程崩了、重启了都不能丢；Postgres 有连接池（`max_size=20`）、事务保证、可审计。Redis 做这种"不能丢的状态"有主从切换丢数据的风险（见 Q4）。

3. **长期记忆 = 跨会话复用的用户资产**
   本项目用 `AsyncPostgresStore`，按 namespace 切：`("user_profiles", user_id)` 存偏好，`("travel_history", user_id)` 存出行历史。是**结构化 KV**，不是向量碎片——读出来直接拼 Prompt（`format_memory_for_prompt`）。
   另外有一个独立维度：**RAG 知识库记忆**（旅游攻略文档）走 Chroma 向量库，那是"公司知识"，不是"某用户的记忆"，两者别混。

### 为什么这么选型（面试爱追问）
| 层 | 存储 | 选它的理由 |
|---|---|---|
| 瞬时 | LLM context | 最快、模型原生读得到，容量天然受限 |
| 会话/Checkpoint | Postgres | 事务持久化、可恢复、能审计；不能用 Redis 当唯一真相 |
| 长期·用户记忆 | Postgres Store（结构化） | 偏好是少量强结构数据，KV 直接读，不用每次做向量召回，省钱准 |
| 长期·知识库 | Chroma（向量） | 文档多、语义模糊，靠 embedding 相似度召回 |

### 必踩的坑（主动讲 = 加分）
> "很多人把 LangGraph Checkpoint 当长期记忆用——它俩不是一回事。Checkpoint 存的是'当前任务跑到哪了'，任务结束就该清理；长期记忆存的是'这个用户是谁、喜欢什么'，跨会话复用。我项目里这两个是分开的：`checkpointer.py` 和 `store.py` 两个 Manager。"

---

## Q2. 多租户向量库怎么隔离？逻辑隔离的安全风险是什么？怎么防越权？

### 先诚实定位现状
本项目现在是**单租户**：Chroma 只有一个 collection `travel_guides`，用户记忆的 namespace 里也只有 `user_id`，没有 `tenant_id`。下面是生产化多租户方案。

### 三种隔离级别（按规模选）
1. **Collection / Schema 隔离**：每个租户独立 collection（Chroma）或独立 schema（Postgres）。隔离最硬，租户少（<100）或大客户私有化时用。
2. **逻辑隔离（推荐，SaaS 主流）**：共享 collection，每条向量 metadata 强制带 `tenant_id`（+ `user_id` / `agent_id` / `rbac_scope`），**检索时把 tenant filter 下推到向量库**。
3. **物理隔离**：独立向量实例，金融/强合规大客户，成本最高。

### 逻辑隔离的安全风险（重点答）
1. **只在应用层过滤、检索层漏过滤 = 越权黑洞**。向量检索语句如果没带 `where tenant_id=?`，靠后端拿到结果再 filter，那别的租户的向量已经被算相似度捞出来了——尤其 embedding 是同一个模型时，跨租户语义相近的片段会混在一起。
2. **缓存越权**：本项目现在 `rag:cache` 的 key 是 `md5(query + top_k)`，**没带 tenant_id/user_id**——多租户上线后，A 用户的检索结果会被 B 用户命中，这是真实的越权漏洞。生产 key 必须拼成 `md5(tenant_id + user_id + query + k)`。
3. **metadata 被篡改/缺失**：写入时漏打 `tenant_id` 标签，这条向量就成了"无主数据"，后续过滤都捞不到或被乱捞。

### 怎么防
- 向量库 SDK 层封装一个强制方法：所有查询必须传 `tenant_id`，不传直接抛错，不允许裸调 similarity_search。
- 写入时 `tenant_id` 由网关注入（从 JWT 拿），不信任前端传值。
- 记忆加 `rbac_scope`：`user`（个人偏好）/ `agent`（该 Agent 共享）/ `team` / `tenant`，召回时 `where scope in (...)`。
- 缓存 key 带租户维度；审计日志记录谁读了哪条记忆。

---

## Q3. 为什么记忆抽取要异步？Kafka 重复消费怎么办？

### 现状诚实说
本项目目前是**同步写**：用户说"我吃不了海鲜"，Agent 调 `update_dietary_restriction_tool` 直接 `aput` 进 Store。MVP 阶段这样最简单。生产化会改异步。

### 为什么要异步（答理由）
1. **记忆抽取本身要调一次 LLM**：判断"这句话值不值得记、是偏好还是闲聊、重要度几分"，一次几百 ms 到几秒。如果同步做，用户流式输出会被卡住。
2. **主链路只对用户响应负责**：用户要的是旅行方案，记忆写成功失败不该阻塞他。对话结束后投一条消息到 MQ，消费者慢慢抽。
3. **削峰 + 可重试**：embedding/写库失败可以重试，不影响在线请求。

### Kafka 重复消费怎么处理（at-least-once 必然重复）
Kafka 语义是至少一次，消费者重试、rebalance 都会重复投。方案：
1. **幂等写**：本项目 Postgres Store 用 `namespace + key` 做主键，重复 `aput` 同 key 就是覆盖，天然幂等——这是结构化 KV 的好处。
2. **自然键去重**：消息体带 `message_id`（或 conversation_id + turn 序号），消费前查一眼有没有处理过。
3. **本地消息表 / 事务消息**：DB 提交和发消息两边一致性。
4. 死信队列：多次失败进 DLQ，人工/补偿处理，别无限重试打爆 embedding。

---

## Q4. Redis 做会话存储会遇到什么问题？持久化怎么处理？

### 本项目里 Redis 的角色
注意：本项目 Redis **不是会话存储**，只做 RAG 检索结果缓存（`rag/cache.py`）。会话/Checkpoint 我放在 Postgres。这题正好讲清楚为什么。

### Redis 做会话会踩的坑
1. **可能丢数据**：Redis 主从复制是异步的，主节点宕机还没同步到从节点的那部分 session 就没了。会话状态不能丢。
2. **内存贵**：活跃 session 多了，每个 graph state 可能几十 KB～几 MB，全堆内存成本高。
3. **大 key 阻塞**：一个 thread 的整个 state 序列化成一个大 hash，读写会阻塞事件循环。
4. **驱逐风险**：内存满了触发 `allkeys-lru`，把"很久没访问但用户明天还要继续"的 session 淘汰了。
5. **缺事务**：跨多个 key 的状态更新要么成功要么回滚，Redis 不擅长。

### 持久化怎么选
- **RDB**：定时快照，恢复快，但两次快照之间的写会丢。
- **AOF**：追加日志，`appendfsync everysec` 最多丢 1 秒，数据安全但文件大、重放慢。
- 会话/缓存这种可重建的数据：RDB + AOF everysec 够了。
- **不能丢的真相状态（checkpoint、订单）**：本项目直接落 Postgres，不赌 Redis 持久化。Redis 只放"丢了能重算"的缓存。

> 一句话收尾："所以我项目里 Redis 当缓存、Postgres 当状态真相，分层就是按'能不能丢'来分的。"

---

## Q5. 向量检索 TopK 召回后为什么还要重排？

### 本项目实况
`app/rag/reranker.py` 里做了两层：
1. `LLMReranker`：qwen-turbo 对召回文档逐篇打 0-10 分，重排取 top 3；
2. `LongContextReorder`：基于 "Lost in the Middle" 研究，把最相关的放开头、次相关放结尾、不相关夹中间。

### 为什么需要重排
1. **向量召回是粗排**：bi-encoder 把 query 和 doc 分别编码成向量算余弦，快但粗——"语义像"不等于"真答这个问题"。比如 query"成都火锅推荐"，可能召回一篇讲成都旅游概况的文档（向量很近但没用）。
2. **精排贵但准**：cross-encoder（或 LLM）把 query+doc 拼一起联合打分，能真正理解"这篇能不能答这个问题"，但慢。所以先向量召回 Top N（比如 20），再精排取 Top K（3）——**精度换成本**。
3. **Lost in the Middle**：LLM 对长上下文中间位置的内容注意力明显弱，所以即使重排完，也要把最相关的放首尾位置。

### 生产演进（诚实讲）
> "我现在用的是 LLM 打分，精度够但每篇都调一次模型、有成本和延迟。生产环境会换成 bge-reranker 这类 cross-encoder 小模型，便宜快，离线部署；LLM rerank 只放在高价值 query 上。"

---

## Q6. 租户记忆无限膨胀怎么治理？（配额 / 衰减 / 归档）

### 本项目的天然优势
本项目长期记忆是**结构化聚合对象**（一个 user 一个 profile、一个 history），不是"每条对话切一片存向量"。所以它根本不会像向量碎片记忆那样无限增长——这个设计本身就是治理。

### 生产化治理三件套
1. **配额（写入前挡）**
   - 租户级：记忆条目上限、embedding 月调用额度、检索 QPS 限流。
   - 用户级：每人 N 条长期记忆，超了就走 LLM 合并旧记忆而不是新增。
   - 本项目可加：写入前 `COUNT` 一下当前 user 条目数。

2. **衰减（软遗忘，不物理删）**
   - 打分：`score = importance * exp(-k * Δt) * log(access_count + 1)`
   - 越久没用、越少被命中，排序越靠后；重要度高的（比如"海鲜过敏"这种健康禁忌）永远高权重不衰减。
   - 本项目里饮食禁忌这类应该打高 importance、不参与衰减。

3. **归档 + 过期**
   - 低分记忆迁冷存储（对象存储/冷表），不再进向量检索。
   - 租户可配 `retention_days`，到期自动标记过期；合规要求删数据时软删除（`is_deleted=true`），异步清向量，保留审计。

### 收尾金句
> "记忆系统不是存得越多越好——存了十年没用的偏好反而会污染当前 Prompt。工程上要像 Redis 一样有淘汰策略，只是这里淘汰的是信息的权重，而不是直接删。"

---

## 面试前自检清单
- [ ] Checkpoint ≠ 长期记忆（能讲清区别）
- [ ] 能画出现项目：Postgres(checkpoint) + Postgres Store(用户记忆) + Chroma(知识库) + Redis(RAG缓存)
- [ ] 多租户：知道现状是单租户，能讲生产方案 + 缓存 key 越权这个真实坑
- [ ] Kafka 没上线：讲"当前同步写，生产异步化"，别吹成已上
- [ ] 重排：能说清 bi-encoder 粗排 vs cross-encoder/LLM 精排 + Lost in the Middle
