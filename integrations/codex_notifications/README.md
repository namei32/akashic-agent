# Windows Codex → WSL Akashic

Windows 桌面 Codex 的 `Stop` Hook 把本轮会话 ID、回合 ID、工作目录和最后回复经
`wsl.exe` 交给 `bridge.py collect`。收集只写 WSL 本地 SQLite，退出时返回 `{}`；
没有网络等待，不读取 Codex transcript、数据库或认证信息。摘要限 6000 字符。

`bridge.py run` 是独立的 WSL 用户服务，通过 Akashic Python SDK 连接现有 Unix
socket，提交 `codex/notifications/submit`。Akashic 需先安装同仓库的
[`codex_notifications`](../../plugins/codex_notifications/README.md) 插件。

所有命令都需要 `--state /absolute/path`：

- `collect`：从 stdin 读取官方 Hook JSON，只处理 `Stop`。
- `run --endpoint /path/to/akashic.sock`：持续转发，失败退避，离线保留。
- `drain --endpoint /path/to/akashic.sock --now`：手动立即处理待发送项；退出 1 表示
  仍有待发送或受阻记录，不表示已发送记录被回滚。
- `status`：查看计数、事件身份和错误，不打印回复正文。
- `retry --event-id ID`：人工排除原因后重新尝试受阻事件；已经提交的事件不重发。

发件箱固定接收端，不能通过改 endpoint 将积压事件改投另一实例；需要另一个实例时
使用新的 state 目录。第一次持久化后，事件正文与时间不再更改。相同事件重复触发
不会新增记录，正文冲突明确失败并保留原记录。ACK 丢失时沿同一事件重投，由 Akashic
返回原消息；SQLite 回执、失败记录和已提交事件均保留，没有自动清理。

`committed` 表示「Codex 动态」中已保存通知，不等于 Codex 整个任务成功。
会话被删除、格式不兼容或同 ID 正文冲突会保留受阻记录，修复后人工 `retry`。

官方协议与信任要求：[Codex Hooks](https://learn.chatgpt.com/docs/hooks)。保留已有
`notify` 和其他 Hook；新增或修改的 Hook 需要在 Codex 界面审核信任后才会运行。
