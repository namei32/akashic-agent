# Hindsight memory plugin

本地实现的普通 API v3 插件。使用 Hindsight HTTP 服务保存长期记忆，保留 Core 消息日志作为原始依据；不将 Hindsight 服务、数据库或模型驱动塞入宿主进程。

## 已实现

- 只从 `MESSAGE_CATALOG` 和公共 `TURN_PROJECTION` 消费已完成、允许学习的真实对话；尊重旧 suppress 标记与会话学习准入。
- `scope_policies` 支持 global、isolated、off。isolated 按规范化 scope 哈希映射独立 Bank；off 禁止写入，但保留读取所属 Bank 的能力。
- 消费 `proactive-feedback.history.v1`，核对 cursor、event_id、payload_hash；反馈登记和同步任务入队在插件 SQLite 的同一事务中提交。
- 只将 gold/high 且已评分的关联材料投影到 Hindsight。引用、接续及 PA/PUA 分数不被解释为用户满意或永久偏好；低可信记录保存跳过理由。
- 持久保存精确提交材料与原 operation_id。提交前先核查原任务；丢失响应和宿主重启不重新生成任务身份。
- `recall_memory(query, limit)` 与 Wake 的现有工具名/参数保持兼容。仅调用 recall，不调用 reflect，也不自行发送消息。
- 保持 Wake 的历史向量兴趣评分语义，独立于 Hindsight 相关分数；必要时用宿主 embedding 补齐获准对话的固定向量缓存。
- 通过 `context.materials.v4` 贡献 recall 材料，尊重 investigate/alert 对 recall/profile 的排除。
- 保存实际召回材料。恢复同一次调用时复用已保存结果；原文引用从 Core 消息验证，提炼事实明确标注，未知/损坏出处不授予引用。
- 从本地原消息的 recorded_at 排除晚于查询时刻的资料。Hindsight 的 query_timestamp 只作为时间解释锚点，不当成后端硬过滤。
- 插件独占 writer lease；关闭时排空实际工作、释放客户端和本地锁。远端已接纳工作留在原任务下，下一次启动继续核查。

## 配置与启用

默认 `initial_config.json` 中 `enabled=false`。本次实现没有修改正式插件选择或上传真实历史。

通过当前插件安装/开发来源机制选择本目录，并保存下列配置（示例）：

```json
{
  "enabled": true,
  "base_url": "http://127.0.0.1:8888",
  "tenant": "default",
  "bank_prefix": "roxy-personal",
  "ledger_namespace": "default",
  "api_key_env": "HINDSIGHT_API_KEY",
  "feedback_enabled": true,
  "poll_seconds": 5,
  "batch_size": 32,
  "budget": "mid",
  "max_tokens": 4096,
  "max_chars": 12000,
  "scope_policies": {
    "project": {"work": "isolated", "scratch": "off"}
  }
}
```

启用前需要：

1. Hindsight 服务能提供 retain、recall 与 operation status/retry 接口，且支持调用者指定异步 operation_id。插件不自动安装、启动或升级服务。
2. 服务侧单独配置抽取、embedding 和 reranker；认证服务的 key 从指定环境变量取得，不写入本地账本或工具参数。
3. 宿主 EMBEDDINGS 仍用于兴趣评分和主动反馈的非引用匹配，不能因为服务侧有 embedding 就撤掉宿主配置。
4. 切换时通过插件管理器禁用 Akasha 插件，再启用本插件。只设置 Akasha 的业务 enabled=false 不够：其 apply 仍发布同名兴趣服务。两个自动向量记忆提供者和同名服务不能同时发布。
5. 需要反馈闭环时，安装的反馈插件必须提供持久分页历史接口；不存在时普通记忆可运行，但反馈消费者等待该依赖。

第一次启用会为已有的获准完成对话登记同步任务。处理速度由 batch_size、后台服务和模型配置决定；“请求已接收”不等于索引或 observations 已更新。

不同 workspace/用户应选择不同 bank_prefix。后端、范围策略或学习来源变更不能沿用旧账本猜测迁移。采用新的 bank_prefix 和 ledger_namespace，可保留旧账本与旧 Bank；本插件不自动删除、清空或重写旧数据。

## 查看与恢复

- `/hindsight_status`：任务计数、最多 20 条失败/异常任务身份、反馈发现游标与确认应用游标。
- `/hindsight_retry <job_id>`：显式恢复失败/取消的原操作，保留原 operation_id；命令重试会先核查服务端状态。
- 已确认的 operation 被服务端删除/清理时，插件不会假定“旧操作未发生”而再次提取。须核查服务实例、任务保留策略和数据库恢复。
- 后台发现数据身份/格式错误时停止同步并降级健康状态，保留独立的兴趣能力；检索故障明确展示不可用，不冒充空历史。

本地 SQLite 归插件数据目录所有，保存 jobs、反馈 cursor 与 hash、原始材料映射、已交付召回。原始 messages 不被修改。可以备份整个插件数据目录和 Hindsight 数据库；重新 LLM 抽取属于新版本重建，不承诺得到相同事实或事实 ID。

## 本阶段边界

本阶段覆盖自动 retain/recall、反馈同步、Wake 兼容、状态与恢复命令。没有实现 reflect、remember_memory/forget_memory 的新语义、服务端部署、正式切换、图形 Dashboard 或真实模型质量 benchmark。显式用户纠正通过已提交原对话与反馈进入记忆；完整遗忘工具应另行定义，不能将删除文档与抑制原消息混为一谈。

后台维护和每个 Hindsight 实例只持有自身状态，不修改主动反馈、Emotion 或投递数据库。反馈消费者使用来源明确的原对话，不用截短 preview 冒充完整证据。

## 验证

```sh
.venv/bin/python -m pytest -q tests/test_hindsight_memory.py tests/test_wake_execution.py
.venv/bin/python scripts/plugin_boundary.py check
uvx --from ruff ruff check plugins/hindsight tests/test_hindsight_memory.py
uvx --from pyright pyright --project pyrightconfig.json plugins/hindsight
```

测试使用真实 Core MessageLog、插件 CompositionRoot/材料机制、Wake 决策回执及受控 HTTP 服务；不会调用真实模型。受控 reader 验证用户纠正的证据改变了下一次 Wake 决定，不代表真实模型的推荐质量已经验证。

上游协议依据：[retain](https://hindsight.vectorize.io/developer/api/retain)、[recall](https://hindsight.vectorize.io/developer/api/recall)、[operations](https://hindsight.vectorize.io/developer/api/operations)、[反馈历史](https://github.com/akashic-plugins/proactive_feedback#durable-history-pull)。
