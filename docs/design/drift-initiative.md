# Drift 主动意图与工作执行

状态：逐步实施。已接入默认关闭的定时交流机会、Wake 判断与真实送达链路；自主规划工作和执行工作尚未接入。

## 目标和边界

用户要求 Agent 能主动搭话，并在长期授权范围内先完成小工作，再带真实结果联系用户。
Drift 拥有主动意图和推进状态；Sources/Reply 执行普通 Agent 工作；Wake 判断交流和投递；Ledger 保存真实消息，Delivery 保存实际发送回执。
依据 PRO-001～PRO-003、STA-001～STA-003、ADR-0102 与 ADR-0111，Core 不增加 Drift 业务分支。

```text
┌────────────────────────┐       ┌────────────────────────┐
│ Timer / 真实对话完成    │ ────▶ │ Drift 评估与意图        │
└────────────────────────┘       └───────────┬────────────┘
                                     搭话  │  工作
                         ┌─────────────────┴──────────────┐
                         ▼                                ▼
              ┌───────────────────┐          ┌────────────────────────┐
              │ 已持久化交流提案  │ ◀─────── │ 普通 Source/Reply 工作 │
              └─────────┬─────────┘  结果引用 └────────────────────────┘
                        ▼
              ┌───────────────────┐
              │ Wake → Delivery   │
              └───────────────────┘
```

## 当前已实现的控制

Drift 发布以下 Gateway 扩展方法：

- `drift/initiative/status`：只读暂停状态、控制版本和既有提案状态计数，不调用模型。
- `drift/initiative/pause`、`drift/initiative/resume`：要求 `request_id` 和 `expected_version`。
- `drift/initiative/config`：读取生产者配置和当前配置输入版本 `input_ref`。
- `drift/initiative/configure`：用 `request_id` 和 `expected_input` 提交完整配置，交由插件配置 owner 校验、发布和换代。

暂停状态与回执在同一个 SQLite 事务提交。相同 ID、相同参数重放原回执；相同 ID 的不同参数或过期控制版本明确拒绝。响应同时返回当前状态，旧暂停回执不能覆盖后来已经恢复的意图。重启保留控制状态，损坏记录报错，不重置为默认值。

新主动提案绑定 channel、recipient 和 session；Wake 只能读取匹配当前目标的提案，领取时再核对目标和暂停状态。模型阶段的持久输入和 Delivery 的首次 started 提交与暂停写入按同一 SQLite 写锁排序：暂停提交后，新主动阶段或发送保留原请求并让出 Wake 循环；恢复通知或重启后读取同一请求继续，不阻塞其他告警。已经接纳的阶段或已经开始的发送按原回执结算。旧提案仍沿原合同处理。

## 定时交流机会

`auto_propose` 默认为 `false`。显式设置为 `true` 后，Drift 的子能力只在 Timer 和已启用 Wake 的联系目标可用时启动；每次等待默认 1800 秒，范围为 60 秒至 7 天。默认只在 Wake 时区的 09:00（含）至 22:00（不含）产生机会，可配置 `start_hour`、`end_hour`。重启后重新等待一个间隔，不补发停机期间的次数。

一个目标最多保留一条未过期的未完成机会。每个机会最晚在一个小时或当日窗口结束时过期，快照、领取和发送前都会检查；过期行保留作为记录，不阻塞新机会。Wake 用目标范围内最近的真实对话和已送达主动历史决定分享或跳过，并要求不编造用户事实或工作结果。模型决定和 Delivery 回执是不同事实，只有真实送达后才结算提案。

例如，先调用 `drift/initiative/config` 取得 `input_ref`，再向 `drift/initiative/configure` 提交 `request_id`、`expected_input`（该 `input_ref`）以及 `auto_propose`、`interval_seconds`、`start_hour`、`end_hour`。配置更新采用插件配置 owner 的回执；当前没有单独的 Drift 设置页面。Wake 仍依赖其现有模型、发送渠道、EventMail 和 Akasha 兴趣能力。

## 持久化和迁移

当前对象是 Drift 自己的 `data_root/drift.sqlite3`，旧 `drift/drift.db` 不在本批迁移范围。

| 对象 | 正常增加与更新 | 逻辑失效及物理减少 | 恢复证据 |
|---|---|---|---|
| 原 `proposals` | 沿原提案、领取、状态流转及送达结算合同 | 本批不改旧行，也不新增减少协议 | 原行全部列、领取身份和结算引用 |
| `initiative_control` | 新库或迁移建立一条未暂停、版本 0 的记录；明确控制操作更新 paused 并递增版本 | 不自动删除、重置或随重启恢复 | 同事务操作回执、数据库一致备份 |
| `initiative_control_receipts` | 每个请求 ID 追加一次固定参数与结果 | 当前不得自动修改或减少 | 请求 ID、参数、原回执 |
| `initiative_proposals` | 与新主动提案同事务保存固定目标；同目标最多一条未过期的未完成提案 | 不原位更改目标；提案状态沿原 owner 流转，过期条目不再被选择 | 当前不得自动减少；目标引用与原提案、控制记录在同库恢复 |

内置迁移 `20261028_01_initiative_control` 从当前 v2 原地增加控制表，事务内比较全部旧提案列值后才提交 schema v3。对仍为 v1 的库，迁移先验证并保留原状，Drift 启用时由原 owner 在同一事务内完成既有 v1 升级和控制表初始化；原有的孤立投递提案失效规则保持不变。缺库不建空业务库，由实际 owner 初始化；未知版本、额外表或完整性错误明确失败。迁移仅通过本 bundle 的 data_root 访问数据，不扫描其他插件目录。

后续 `20261029_01_initiative_target` 从 v3 只增加目标表并核对旧提案、控制和回执的全部行；v1 延后由原 owner 一次升级到 v4。旧 v2/v3 代码不识别 v4；回退应使用保留 v4 读取能力、关闭新主动生产的兼容版本。不得用旧会话备份覆盖后续新增消息，也不自动清理新记录。

Wake 消费的 Drift 合同升级为 `drift.wake.v3`，以显式传递目标并保护阶段和发送准入；该合同变化需要重新启动使用它的插件组合。控制提交后的变化通知只唤醒重查，SQLite 仍是唯一事实来源。
Wake 使用 `delivery.guarded-start.v2` 在恢复被暂停的发送时重新检查准入；其他消费者继续使用 v1。

## 后续接入

1. 读取目标范围上下文，显式召回记忆；内部规划消息使用 drift 身份、internal、learning=excluded。
2. 产生 none/chat/work 决定；只有交流或已完成工作结果进入 Wake 提案队列。
3. 工作复用 Source/Reply，按明确授权限定工具与资源；工作完成和消息送达分别依据真实回执。
4. 增加设置页面、检查原因和产物入口；停用 Drift 不影响 Wake 其他职责。

## 验证

`python scripts/drift_controls_scenario.py` 在临时 workspace 中使用实际旧版 owner 构造提案，经过真实 Yoyo runner、CompositionRoot、Gateway 方法和 SQLite 核对迁移、请求重放、控制竞争与重启。独立 Ledger 原消息逐列比较。`python scripts/drift_target_scenario.py` 检查目标隔离及跨代际暂停竞争和恢复。`python scripts/drift_producer_scenario.py` 以真实 Timer、Wake 和本地测试接收端验证机会、暂停、告警、恢复和过期，不连接正式渠道。

静态检查包括 Python 类型、插件边界、迁移追加约束和 diff。隔离场景不代表后台工作、正式渠道发送或正式实例迁移已经完成。
