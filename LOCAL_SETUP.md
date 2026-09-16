# 本机学习与开发

本次安装使用 macOS、Python 3.12 和独立虚拟环境。Web 入口为
<http://127.0.0.1:2238>，仅供本机访问。旧 SSH 隧道使用 2236，另一个现有服务使用
2237，因此学习版独立使用 2238。

网页导航现在包含“对话”“工作台”“知识与运行”“模型”：

- 工作台：<http://127.0.0.1:2238/#workbench>，读取会话和消息记录。
- 知识与运行：<http://127.0.0.1:2238/#runtime>，读取文档、能力目录和定时任务。
- 模型管理：<http://127.0.0.1:2238/#models>。

Android 配对步骤见 [MOBILE_SETUP.md](MOBILE_SETUP.md)。本机移动网关已按 Tailscale 私有网络配置。

已补充安装 `scheduler`、`workbench-ui`、`runtime-ui`。没有配置外部 MCP、Skills
或定时任务时，相应目录为空属于正常状态。

## 仓库与分支

- `origin`：<https://github.com/namei32/akashic-agent>，自己的 Fork。
- `upstream`：<https://github.com/kachofugetsu09/akashic-agent>，原作者仓库。
- `main`：保留上游版本。
- `my-changes`：本次部署修复和后续个人修改。
- 本次上游基线：`b3953f0ea4b4b184c31bb6a4c7e32b067c627e3b`。

本仓库单独配置了 GitHub noreply 提交邮箱，不依赖系统推断的本机邮箱。

## 启动、停止与日志

服务由当前用户的 macOS LaunchAgent 管理，登录后自动启动，进程退出后自动重启。
查看进程及 Web 就绪状态：

```bash
"$HOME/.local/share/akashic-learning/service.sh" status
```

统一启停入口：

```bash
"$HOME/.local/share/akashic-learning/service.sh" start
"$HOME/.local/share/akashic-learning/service.sh" stop
"$HOME/.local/share/akashic-learning/service.sh" restart
```

`stop` 暂停当前登录期间的服务，下次登录仍自动启动。要同时取消自动启动：

```bash
"$HOME/.local/share/akashic-learning/service.sh" disable
```

再次执行 `start` 会重新启用自动启动。不要用单纯杀进程来停服，否则 launchd 会将它拉起。

查看实时日志：

```bash
"$HOME/.local/share/akashic-learning/service.sh" logs
```

按 Ctrl+C 仅停止查看日志。需要前台调试时，先用 `service.sh stop` 停止常驻服务，
然后运行 `$HOME/.local/share/akashic-learning/start.sh`。

这些脚本位于本机数据目录，记录了当前源码目录的位置；移动源码目录时，需要同步调整
脚本及 LaunchAgent 的工作目录。LaunchAgent 文件为
`$HOME/Library/LaunchAgents/io.namei.akashic-learning.plist`。

## 持续运行的范围

- 原有 Akashic Supervisor 管理内部 Gateway；launchd 管理 Supervisor 进程。
- `KeepAlive=true` 在进程退出后重新启动，`ThrottleInterval=10` 避免频繁启动循环。
- 关闭浏览器、终端或 Codex 不影响服务；退出 macOS 用户会话后会停止，下次登录恢复。
- 当前保留系统休眠设置。睡眠、合盖、关机、断电、网络中断或模型认证失效，仍可能中断使用。
- 若要接通电源时防止系统自动休眠，可将本机 `service.json` 中的
  `keep_awake_on_ac` 改为 `true`，然后 `service.sh restart`。它使用系统
  `caffeinate -s`，只在该进程运行且接通电源时生效，不阻止屏幕熄灭。
- launchd 负责退出恢复，不能判断所有“进程仍在但功能卡住”的情况；`service.sh status`
  会额外检查 HTTP 就绪状态。需要脱离电脑状态的 24 小时服务，应部署到常开设备或 Linux 服务器。

系统机制参考：<https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html>。

2026-09-16 已验证：工作台会话与消息读取、三个运行目录接口、`service.sh restart`，
以及向托管进程发送 SIGTERM 后由 launchd 自动拉起；本次约 4.2 秒恢复 Web 和聊天就绪。
恢复证据保存在本机 `service-recovery-check.json`。

## 数据与模型

所有本次部署数据保存在 `$HOME/.local/share/akashic-learning/`：

| 路径 | 内容 |
| --- | --- |
| `config.toml` | Core 配置 |
| `service.json` | 本机端口及接电防休眠选项 |
| `workspace/` | 会话、记忆、模型配置和插件数据 |
| `plugin-home/` | 安装的插件制品与清单 |
| `releases/` | 固定提交构建出的插件 bundle 和 Web 资源 |
| `logs/` | 构建、安装与运行日志 |

开发使用独立的 workspace 和 plugin-home。模型连接名为“Codex 本机登录”，
通过模型插件的 RPC 导入现有登录并同步模型，默认选择 `gpt-5.6-sol`。
原 Codex 登录文件未修改。凭据保存在本机模型数据库中，不在 Git 仓库里。

如果重新登录 Codex 后需要再次同步，先启动 Akashic，再在仓库根目录执行：

```bash
PYTHONPATH="$PWD" .venv/bin/python \
  "$HOME/.local/share/akashic-learning/import-codex-login.py"
```

启动脚本保留 macOS 系统网络代理，并让 localhost 请求直接连接本机。

## 本次处理的部署问题

1. 用项目的 `scripts.upgrade_plugin_selection` 初始化缺失的插件组合记录，并保存恢复点。
2. 在默认 profile 之外安装 `codex`、`shell-ui`、`conversation-ui`。
3. 通过插件配置将 Web Chat socket 对齐到 Supervisor 的 `web-chat.sock`。
4. 修复 Codex 对冻结消息、嵌套工具参数的 JSON 序列化。
5. 将 Codex 请求中的 `client_metadata` 放到请求顶层，并去除该后端不支持的
   `max_output_tokens`。Codex 后端管理输出上限，这个字段无法作为请求级硬限制。
6. 当前上游在发布插件后可能遗留旧安装缓存指针。本次仅在停止服务、持有锁并核对
   制品哈希等于已提交组合后对齐安装元数据，原指针保存在数据目录的备份里。

第 4、5 项为 `my-changes` 分支的源码修改。没有改动原作者仓库。

相关回归测试（在仓库根目录执行）：

```bash
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost \
  .venv/bin/python -m pytest \
  tests/test_codex_frozen_requests.py tests/test_model_execution.py \
  -k 'frozen_request or (reuses_socket and codex)' -q
```

## 后续修改与同步

日常在 `my-changes` 上修改，按功能提交。同步上游前先提交或妥善保存当前改动：

```bash
git fetch upstream
git switch main
git merge --ff-only upstream/main
git push origin main
git switch my-changes
git merge main
# 处理冲突、运行相关测试后
git push origin my-changes
```

Core 从当前源码目录运行；业务插件从安装后的固定制品运行。因此修改
`plugins/` 源码后，需要提交、重新构建对应 bundle，再通过插件安装和发布流程更新。
单纯重启不会让已安装插件自动采用仓库里的新代码。

阅读代码可从 `main.py` → `bootstrap/app.py` → `agent/plugin_composition/`
开始，再看 `plugins/` 下的具体功能；前端入口在 `frontend/`。

正式部署到 Linux 服务器时，应使用自己的 Fork 和已测试的完整 commit SHA，
重新准备目标机的配置、数据目录、插件组合和服务管理。该步骤尚未执行。
