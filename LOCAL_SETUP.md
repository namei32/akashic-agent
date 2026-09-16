# 本机学习与开发

本次安装使用 macOS、Python 3.12 和独立虚拟环境。Web 入口为
<http://127.0.0.1:2236>，仅供本机访问。

## 仓库与分支

- `origin`：<https://github.com/namei32/akashic-agent>，自己的 Fork。
- `upstream`：<https://github.com/kachofugetsu09/akashic-agent>，原作者仓库。
- `main`：保留上游版本。
- `my-changes`：本次部署修复和后续个人修改。
- 本次上游基线：`b3953f0ea4b4b184c31bb6a4c7e32b067c627e3b`。

本仓库单独配置了 GitHub noreply 提交邮箱，不依赖系统推断的本机邮箱。

## 启动、停止与日志

在终端运行以下脚本可前台启动；退出时按 Ctrl+C：

```bash
"$HOME/.local/share/akashic-learning/start.sh"
```

停止后台运行的学习实例，在仓库根目录执行：

```bash
.venv/bin/python "$HOME/.local/share/akashic-learning/stop.py"
```

查看本次后台启动日志：

```bash
tail -n 60 "$HOME/.local/share/akashic-learning/logs/runtime.log"
```

这些脚本位于本机数据目录，记录了当前源码目录的位置；移动源码目录时也要调整
`start.sh` 中的 `cd` 路径。本次没有配置开机自动启动。

## 数据与模型

所有本次部署数据保存在 `$HOME/.local/share/akashic-learning/`：

| 路径 | 内容 |
| --- | --- |
| `config.toml` | Core 配置 |
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
