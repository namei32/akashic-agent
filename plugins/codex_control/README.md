# Windows Codex CLI 控制

在 Akashic 现有聊天中指挥 Windows 原生 Codex CLI：新建项目任务、选择已有聊天、
发送文字与真实图片、读取结果、调整或中断回合。CLI 独立运行，桌面可以继续打开或
完全不启动。`codex_notifications` 仍独立接收 Stop 通知，普通聊天不会自动转发。

## 启动与连接

Windows 启动器 `integrations/codex_control/Start-CodexControl.ps1` 查找原生 `codex.exe`：
优先显式 `-CliPath`，其次 PATH，再查现有用户 CLI 目录和已安装桌面包中的 CLI。
有独立 CLI 时不要求安装桌面。后端保留当前用户的 Codex HOME、账号、配置、模型与
权限策略，只监听 `127.0.0.1:45873`，不添加 sandbox 或 approval bypass 参数。

```powershell
powershell.exe -NoProfile -File .\Start-CodexControl.ps1 -Check
powershell.exe -NoProfile -File .\Start-CodexControl.ps1
```

`-Check` 只读；`-Wait` 等待后端结束，供监督进程使用。重复启动先核对 PID、创建时间、
CLI、HOME 和端口回执，身份失配则拒绝复用，不终止未知进程。启动器不操作桌面进程。

可在稳定安装目录运行相邻的 `Install-CodexControlTask.ps1`，注册当前用户的
`Akashic Codex Control` Windows 计划任务并启动。任务在用户登录后启动，以普通权限
运行；失败后每分钟重启一次，最多三次。`-Check` 查看任务状态与最近退出码。
任务不是系统级开机服务，用户未登录时不会运行；崩溃后未完成命令仍须读取实际状态。

若当前执行策略拒绝本地 `.ps1`，先审核文件并取得操作者允许。计划任务使用进程范围
的 `-ExecutionPolicy RemoteSigned`，不修改永久策略；启动器在创建 Codex 子进程前移除
该临时覆盖。测试必须显式使用 `-CodexHome <隔离目录>`、不同端口及 StateDirectory。

通过 Akashic 正式插件安装链安装 `plugins/codex_control`，默认配置：

```json
{"endpoint":"ws://127.0.0.1:45873"}
```

只接受 loopback `ws://`，要求 WSL 与 Windows localhost 互通。端口仅供本机受信任进程，
不开放 LAN 或公网。上游 WebSocket transport 仍是实验性，CLI 升级后须重新验证。
无需桌面内部环境变量、按键模拟或直接读写 Codex 数据库。

## 聊天用法

- “在 Windows 的 `C:\Users\namei\project` 新建 Codex CLI 任务。”
- “把这段要求和我上传的图片发给它。”
- “列出 CLI 的聊天，选中这一个，以后这个 Akashic 会话就指挥它。”
- “读一下结果。”“把正在运行的任务调整为……。”“停止这个回合。”

工具 `codex_control` 提供 `create/list/select/read/send/steer/interrupt/approve`。
`create` 需要用户指定的 Windows 绝对 `cwd`，创建后选中该聊天，不立即执行工作。
`send` 开始新回合；忙时不偷偷改成 steer 或中断。steer、interrupt 绑定准确 turn ID。
工具不覆写模型和权限策略；审批只响应用户对当前准确 request ID 的明确决定。
当前支持命令执行和文件变更审批，其他客户端交互明确拒绝。

独立 CLI 无法观察或调整另一个桌面后端正在执行的回合。已有历史可明确选择后续接，
但不要同时从 CLI 和桌面续接同一聊天；默认新建 CLI 任务最直接。

图片 ID 只接受本 Akashic 会话已提交 Input 的附件。插件通过 Ledger 的有界读租约取得
原字节，以 data URL 传输，不接受模型编造的文件路径或附件 ID。最多 4 张、合计 10 MiB，
支持 PNG/JPEG/WebP/GIF；发送前验证实际格式、完整性与 5000 万像素上限，失败明确返回。

`accepted` 表示远端接纳，完成由真实 turn 状态确认。启用 jobs 时，完成通知唤醒原
Akashic 会话，再用 `read` 读取结果；也可主动读取。断线、超时或丢 ACK 不自动重发。
`read` 展示最近三轮的状态、回复、命令和文件改动摘要，裁切有明确标记。

## 状态与恢复

插件只在自己的 Ledger owner_records 中增加 `command:<effect-key>` 回执，更新
`target:<akashic-session>` 目标；较旧调用不能覆盖较新选择。新建任务和发送前先保存
started，相同调用仅回读原回执，未知结果不重放。没有 schema 迁移或旧消息改写。
图片仍由原附件 owner 保管，不额外保存一套媒体库。

最多 16 个待审批项和 128 个完成状态是临时观察数据，断开或卸载时释放；不停止 Windows
后端。重连后读取真实聊天状态。Windows 的 `%LOCALAPPDATA%\AkashicCodexControl` 保存
PID/CLI/HOME/端口回执和启动日志，日志没有自动清理。源码回退不撤销已发送指令或文件
变化；停止计划任务不等于撤销 Codex 工作，应先通过正常中断入口处理活动回合。

## 验证

`scripts/codex_control_scenario.py` 使用真实原生 CLI app-server、隔离 CODEX_HOME 与本地
Responses 服务。`--with-plugin` 再经正式 Git 安装器、真实 Root、Ledger 和工具 binding
验证新建任务、续聊、原图、重复回执、丢 ACK 和插件重启；正式账号与聊天不参与。

```sh
python scripts/codex_control_scenario.py --codex-cli /path/to/codex.exe \
  --directory /mnt/c/Users/<user>/AppData/Local/Temp/<new-name> --with-plugin
```

目录须为 Windows 可访问的一次性新目录。正式激活另核对计划任务、Windows 平台身份、
WSL 可达、实时插件选择，以及专用验收任务的实际完成状态。

官方协议：[Codex App Server](https://learn.chatgpt.com/docs/app-server)。
