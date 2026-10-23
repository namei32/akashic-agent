# Codex 动态

将桌面 Codex 的 Stop 事件写入固定的「Codex 动态」会话。通知表示本轮有结果，
不保证整项任务成功；用户可以在这个会话继续询问 Akashic。

安装此可选插件后，已有私有 Gateway 提供两个方法：

- `codex/notifications/status`：只读目标身份、是否已创建及是否删除，不创建空会话。
- `codex/notifications/submit`：接收 `events.Event` 定义的严格 v1 事件。接纳后返回
  `event_id`、`session_id`、`message_id`、`seq` 和 `status=committed`，客户端提醒结果
  单列 `notification_status`。重复提交相同事件返回原消息，改变正文会拒绝。

调用方必须先持久化事件和时间，再发送。只有收到匹配事件身份的 committed 回执，
才能标记本地已提交。连接错误不证明服务端没有保存，重试继续使用原事件。

插件没有独立 HTTP 监听，不调用模型，不读取 Codex 数据库或认证信息。
会话被手动删除后，通知不会将它恢复；原事件与历史消息均保留。
状态归属及失败边界见 [ADR-0128](../../docs/decisions/0128-codex-notifications-use-local-gateway.md)。

Windows 桌面端与 WSL 的安装、Hook 信任和本地发件箱操作见
[桥接安装说明](../../integrations/codex_notifications/README.md)。
