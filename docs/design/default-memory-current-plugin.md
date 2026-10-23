# 当前插件架构中的 Default Memory

状态：实现与 WSL 迁移验证中。正式运行状态以目标实例的 selection 和补学回执为准。

## 目标与边界

用户要求从 Akasha 切换到 Default Memory，保留全部对话历史，并补学所有符合学习条件的历史。
目标是当前普通插件中的结构化记忆，不恢复已退役的 Core MemoryEngine。

依据 MEM-004～MEM-008、SES-005、SES-010 和 [0073](../decisions/0073-session-scope-routes-akasha-graphs.md)：
新增 `memory/default-v2.db`，不读取、导入或更新旧 `memory2.db`。
消息、身份、顺序、时间、来源、会话归属、附件、Markdown 档案和原 Akasha 图保持原 owner。

## 调用链

```text
┌─────────────────────────────────────────────┐
│ Ledger 原始消息 + TurnProjection + 学习资格 │
└──────────────────────┬──────────────────────┘
                       ▼
┌─────────────────────────────────────────────┐
│ Default Memory：用户证据 → 提炼草稿 → 向量  │
│                 事务提交事实、出处、回执    │
└──────────────────────┬──────────────────────┘
                       ▼
┌─────────────────────────────────────────────┐
│ 余弦/热度 + 关键词 → RRF → Context / Tools │
└─────────────────────────────────────────────┘
```

插件只依赖公开的消息目录、完整 Turn 投影、内容、模型、Context、Tools、Commands 合同。
它没有 Message writer，也没有原始消息数据库路径。

Akasha 新增 `memory_enabled`，默认 true，旧配置行为保持不变。
设为 false 时不占用 embedding memory 槽位、不启动学习图、不注册图召回与反馈工具，
但保留旧 Inspector、项目策略和 Wake 兴趣服务。
Default Memory 使用 Akasha 发布的只读 `MEMORY_SCOPES`，不复制项目策略。
未来项目仍通过原设置入口写 set-once 策略。

支持模式中的 Wake 可为最近符合条件的已完成问答补齐派生向量，继续使用现有兴趣算法，
不建立或更新 Akasha 图。它仍通过 Ledger 的向量存储合同写派生数据。

## 与历史 Default Memory 的关系

保留结构化 profile、preference、procedure、event；向量与关键词两路召回、
RRF 融合、强化/时间衰减、逻辑遗忘及来源追踪。
关键词使用子串匹配及中文片段；不是 BM25，也不读取原始消息作为召回内容。

本次现代化移植有明确差异：

- 自动学习以已完成的合资格 Turn 为单位，历史补学与在线学习共用同一入口。
  不恢复旧 consolidation event bus。
- 每份模型草稿先耐久保存；事实、来源、撤回和完成进度在同一 SQLite 事务提交。
- 旧事实自动失效要求本轮用户明确撤回或纠正的证据；相似主题本身不触发覆盖。
- 相同事实再次出现只强化；同一来源重跑不会重复强化。近七天高度相似事件允许复用。
- 不恢复旧 PENDING、旧 Markdown 优化器或旧 MemoryEngine。
- HyDE 查询扩展、旧 forced-tool 单独预算及 procedure 模糊合并尚未移植；
  本实现不能声称与历史 Default Memory 在所有检索细节上等价。
- Akasha 的 InMind 评测数字不能作为本实现的性能结论。

## 持久对象

| 对象 | 增加与更新 | 失效与减少 | 恢复证据 |
|---|---|---|---|
| memory_items | 提炼或显式 remember 增加；重复来源不重复强化；独立来源可强化 | forget/用户纠正只改 status；当前不得自动物理删除 | 原事实、origins、changes、学习回执 |
| learning | 固定来源摘要、模型与草稿；只从 draft 进入 applied | 失败不推进；不自动删除 | source digest、原消息 ID、模型输出 |
| origins / changes | 事务追加来源关联与逻辑撤回证据 | 当前不得自动减少 | 同库事务 |
| receipts | 自动召回按最新真实 Input ID、工具按实际 CallRef 固定结果 | 后续学习与遗忘不改旧查询；不自动删除 | 固定查询结果与参数摘要 |
| metadata | schema 与 embedding 空间首次固定 | 换模型或维度明确报错；无自动重建 | 数据库备份及原模型配置 |
| 原始聊天及附件 | 继续由 Ledger 正常新增 | 本迁移没有更新或删除权 | SQLite 一致快照、逐列差异及附件摘要 |
| Akasha 图与旧查询 | 保留 | 不清理 | 原文件、owner records、旧发布版本 |
| Markdown 档案 | 继续沿原插件协议 | 本插件不写 | 原 Markdown owner 的恢复点 |

## 失败与并发

只有正式 runtime 启动事件打开数据库 writer 和文件锁；候选初始化不启动补学。
同一轮只有一个学习任务提交；显式记忆修改与学习共用锁。
SQLite 使用 WAL 和 FULL synchronous，读模型服务前不持有 SQL 写事务。

取消保留已保存草稿；恢复先核对来源摘要，再复用草稿和已提交结果。
失败项在状态接口中可见，自动消息唤醒不反复重试同一失败；显式 backfill 可以重试。
向量服务故障可带明确状态使用关键词召回；数据库、数据形状和空间不一致直接失败。
自动召回只在最新真实用户 Input 上第一次准备，并持久复用结果。

## 迁移与回退

1. 核对实际 WSL、运行代码、插件选择及数据路径；保存 SQLite 一致快照和原始历史校验。
2. 在 WSL 的一次性实例验证真实插件、HTTP 模型驱动、完整轮次、项目策略及恢复。
3. 发布固定产物，保留实际部署版其他插件代码；Default 先以 staging 模式接入。
4. 使用实际模型补学全部合资格历史，核对 eligible/applied/failed，检查原消息和附件。
5. 一次发布将 Akasha `memory_enabled=false`、Default `mode=active`，
   然后启用在线学习，验证真实 Context 召回、显式工具和冷启动。
6. 回退只恢复提供者选择与兼容代码，保留新旧记忆数据库和新增对话。
   不用旧 sessions.db 备份覆盖切换后新增消息。

发行版默认关闭 Default Memory；迁移时显式选择 staging，默认不自动调用模型。`/default_memory_backfill` 是明确的全历史模型调用入口，
`/default_memory_status` 展示实际状态，`/default_memory_pause` 暂停后续轮次。
Gateway 提供 `default-memory/status`、`default-memory/audit`、`default-memory/backfill` 同义入口。

## 验收

- 真正的插件装配、Ledger、模型 HTTP driver 与 SQLite 链路中完成学习、召回和显式遗忘。
- 排除会话、未完成轮次和 off 范围不学习；isolated 不串内容，recall=false 拒绝召回。
- 复跑不重复提炼或强化；召回重试、遗忘后的旧查询、冷启动结果符合回执。
- 支持模式不产生 Akasha 图，Wake 能使用新问答向量。
- 对正式快照使用同一学习管线审计，再逐列比较所有 Message、Session 和附件关联；
  单独检查实际附件内容、Markdown 与切换后的新消息追加。
- 静态检查包括类型、插件边界和 diff；不新增单元测试。
