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

## 在目标 WSL 安装

从包含本功能的已提交版本运行。Python 需为已安装 Akashic 开发依赖的 3.12+ 环境；
安装器会为桥接程序单独创建虚拟环境并安装本仓库的无第三方依赖 SDK，不修改正式
Akashic 的 Python 环境。替换 Windows 用户目录和发行版名称：

```bash
python integrations/codex_notifications/install_wsl.py \
  --workspace "$HOME/.akashic/workspace" \
  --windows-codex-home /mnt/c/Users/你的Windows用户名/.codex \
  --distro Ubuntu-24.04 \
  --install-plugin
```

`--install-plugin` 使用当前提交的插件子目录构建正式 Git bundle，经正在运行的
Akashic `plugin/install` / `plugin/update` 确认激活；不升级 Core 或其他插件。
已安装通知插件时可省略此参数。默认工作区需提供 `akashic.sock`。

安装器会：

1. 按内容摘要保存桥接脚本和 SDK，准备专用 Python；已保存制品不允许原位改写。
2. 确认接收插件可用，固定发件箱与接收端关系。
3. 将一个 Stop Hook 合并进 Windows Codex 的 `hooks.json`，保留既有 Hook 与 notify。
   已有文件修改前生成 `.before-akashic-*` 恢复点；相同配置重复安装不新增 Hook。
4. 注册并启动 WSL 用户服务 `akashic-codex-notifications.service`。

Windows Hook 用 `commandWindows` 通过 `wsl.exe --distribution ... --user ... --exec ...`
传递 stdin JSON，不把回复拼进命令。WSL 的普通 command 指向同一收集程序。
**安装之后仍须在 Windows Codex 的 Hooks 界面审核并信任新增定义，再开启新任务验证。**
安装器不会写入 Codex 信任记录或绕过信任审核。

服务依赖 WSL 中已启用 systemd 和该用户的运行会话；使用 `--no-start` 可以只准备
配置。用户服务的实际状态以 `systemctl --user is-active` 为准，不能用安装命令退出
成功代替桌面 Hook 已触发。机器休眠、WSL 停止或尚未信任 Hook 时不能收到新事件。

```bash
systemctl --user status akashic-codex-notifications.service
journalctl --user -u akashic-codex-notifications.service --since today
```

`~/.local/state/akashic-codex/installation.json` 保存当前解释器、脚本、接收端和 Hook
路径，按其中的制品路径运行 `bridge.py status --state ...` 可查看积压。
要停止转发可执行 `systemctl --user disable --now akashic-codex-notifications.service`，
再在 Codex 的 Hooks 界面禁用本通知钩子；发件箱、原消息和恢复点均保留。
