# 国服 LOL 战绩查询

插件提供 `lol_client_status`、`lol_match_history`、`lol_match_detail`。
查询走已登录国服客户端的 LCU 和 SGP，不使用 WeGame 战绩页面。
隐藏生涯不会在插件中触发跳过；接口拒绝、超时、限流和有效空数组分别返回。

```text
┌─────────────────────┐    认证 HTTP    ┌──────────────────────┐
│ Akashic 查询工具    │ ──────────────▶ │ Windows 只读桥接     │
└─────────────────────┘                └──────────┬───────────┘
                                                 ▼
                                      ┌──────────────────────┐
                                      │ 本机 LCU / 腾讯 SGP  │
                                      └──────────────────────┘
```

## 在游戏电脑启动桥接

需要 Python 3.12+。把整个 `lol_match_history` 目录复制到游戏电脑，进入该目录。

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
$env:AKASHIC_LOL_BRIDGE_TOKEN = (.\.venv\Scripts\python -c "import secrets; print(secrets.token_hex(32))")
$lolPython = (Resolve-Path .\.venv\Scripts\python.exe).Path
$lolLockfile = 'D:\League of Legends\LeagueClient\lockfile'
$lolBridgeArgs = 'bridge.py --lockfile "{0}" --platform HN1' -f $lolLockfile
$lolBridgeProcess = Start-Process -FilePath $lolPython -ArgumentList $lolBridgeArgs `
  -WorkingDirectory (Get-Location).Path -NoNewWindow -PassThru
```

`--lockfile` 使用实际 LOL 安装目录里的文件，客户端重启后每次查询重新读取。
示例路径须替换为游戏电脑上的真实路径；先用 `Test-Path $lolLockfile` 确认文件存在。
`--platform` 必须对应当前登录大区；目前支持 HN1（艾欧尼亚）、HN10（黑色玫瑰）、
NJ100/GZ100/CQ100/TJ100/TJ101（联盟一至五区）、BGP2（峡谷之巅）。
当前版本只查询登录所在合区，查其他大区需要切换登录及桥接配置。

桥接默认只监听 `127.0.0.1:22361`。异机运行用 SSH 端口转发，或 HTTPS 反向代理。
WSL 可以按网络模式使用本机转发或 HTTPS 地址。代理须保留 Authorization 头。
桥接只开放三个固定查询入口，不开放任意客户端命令。

## 先在 Windows 上直接验证查询

这一步不需要安装 Akashic。用账号 A 登录国服客户端，选择同一合区内、确实有近期对局的
公开账号及隐藏账号 B。先在 WeGame 中确认 B 显示隐藏；A 与 B 必须是不同账号。

上面的后台进程继承当前 PowerShell 的 token 环境变量，因此下面命令在同一窗口执行。
看到“LOL 只读桥接”启动行后再调用；若之前用前台方式启动，应在另一个窗口设置相同 token。
[Start-Process 的环境继承与参数引用规则](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.management/start-process)
是这段启动命令的依据。当前没有 Windows/国服客户端运行验收证据。

```powershell
$lolBaseUrl = 'http://127.0.0.1:22361'
$lolHeaders = @{ Authorization = "Bearer $env:AKASHIC_LOL_BRIDGE_TOKEN" }
Invoke-RestMethod -Uri "$lolBaseUrl/v1/status" -Headers $lolHeaders
```

状态应返回 `connected: true` 和实际登录账号。然后先查公开账号：

```powershell
$lolBody = @{ riot_id = '公开玩家昵称#编号'; start = 0; count = 5 } | ConvertTo-Json -Compress
$lolPublicReply = Invoke-RestMethod -Method Post -Uri "$lolBaseUrl/v1/history" `
  -Headers $lolHeaders -ContentType 'application/json; charset=utf-8' `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($lolBody))
$lolPublicReply | ConvertTo-Json -Depth 20
```

再查 B，把占位昵称替换为 B 的完整昵称和编号：

```powershell
$lolBody = @{ riot_id = '隐藏玩家昵称#编号'; start = 0; count = 5 } | ConvertTo-Json -Compress
$lolHiddenReply = Invoke-RestMethod -Method Post -Uri "$lolBaseUrl/v1/history" `
  -Headers $lolHeaders -ContentType 'application/json; charset=utf-8' `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($lolBody))
$lolHiddenReply | ConvertTo-Json -Depth 20
```

隐藏查询通过的标准：已独立确认 B 在 WeGame 隐藏，返回 `returned_count > 0`，且用 B 本人可见
的记录或已知共同对局核对 game_id、英雄、胜负和 KDA。HTTP 200、空数组和一句“查询成功”
不能替代这些证据。客户端 privacy 与 WeGame 标记可能不同，见下方字段说明。
取得非空记录后，用返回的 ID 验证单局详情：

```powershell
$lolBody = @{ game_id = [string]$lolHiddenReply.matches[0].game_id } | ConvertTo-Json -Compress
Invoke-RestMethod -Method Post -Uri "$lolBaseUrl/v1/detail" `
  -Headers $lolHeaders -ContentType 'application/json; charset=utf-8' `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($lolBody)) | ConvertTo-Json -Depth 20
```

常见失败：`unauthorized` 检查 token；`client_unavailable` 检查 lockfile 和客户端；
`player_not_found` 检查完整昵称#编号及登录大区；`history_unavailable` 查看 `attempts` 中两种来源的错误。
再重启 LOL 并重新登录同一合区，检查查询能否恢复。验证完且不继续连接 Akashic 时，
用 `Stop-Process -Id $lolBridgeProcess.Id` 关闭本次启动的桥接。

## 在 Akashic 中使用

通过正式插件管理入口安装/选择插件；默认关闭。停止 Akashic 后，在包含宿主 SDK
和本插件的源码环境中，用公共配置 writer 显式开启（data-dir 须为绝对且无符号链接的路径）：

```bash
python -m plugins.lol_match_history.configure \
  --data-dir /绝对路径/workspace/plugin-data/已安装插件的准确ID \
  --enabled --bridge-url http://127.0.0.1:22361
```

修改配置不替代安装或选择；重启后宿主读取新的固定输入。
该入口校验字段，保留未修改字段，并由公共 writer 保存旧输入恢复点。配置内容为：

```json
{"enabled": true, "bridge_url": "http://127.0.0.1:22361", "token_env": "AKASHIC_LOL_BRIDGE_TOKEN"}
```

在 Akashic 进程环境中设置同名变量，值与桥接电脑生成的 token 相同。
不把 token、客户端 lockfile 密码或登录 accessToken 写入普通配置。
初始化、导入及工具注册不连接客户端；实际调用才建立有限时 HTTP 连接。
查询不写战绩数据库，调用结果由既有 Tools owner 保存为工具结果消息。
停用/卸载不删除历史结果或运行数据，源码恢复使用 Git。

对 Akashic 说“查某昵称#编号最近 20 场战绩”；工具也接受 `start` 与 `count` 分页。
安装启用后依次要求调用 `lol_client_status`、`lol_match_history`、`lol_match_detail`，
在实际工具调用轨迹中确认这些工具被执行，并将输出与上面的直接 HTTP 查询核对。
结果包含来源、大区、查询时间、原始 privacy 值、胜负、英雄、KDA、经济和伤害。
`hidden_history_verified=true` 仅在本次取得非空战绩且客户端标记 PRIVATE 时成立。
WeGame 的隐藏标记与客户端 privacy 不一定相同，因此该字段为 false 不表示 WeGame 没隐藏。
空数组不证明隐藏战绩可查；两个查询源都失败时保留各自原因，不伪造战绩。

## 接口依据与验收边界

主机列表、entitlements token 和 SGP SUMMARY 路径参考
[Rank Analysis 的固定源码](https://github.com/wnzzer/rank-analysis/blob/5e9fe0c64233126e3a2c0dc0784dca18e1ddaf08/rank-analysis-app/src-tauri/src/lcu/api/sgp.rs)，
alias 查询参考 [League Akari 的固定源码](https://github.com/LeagueAkari/LeagueAkari/blob/5109b2f7fcce6e02312e534cd1729ed0f2142b51/src/shared/http-api-axios-helper/league-client/summoner.ts)。
接口及字段按这些代码推导，本插件尚未在已登录国服客户端实测。
[Riot 对 LCU 的说明](https://developer.riotgames.com/docs/lol#league-client-api)不保证第三方使用的稳定性。

正式验收需要同一登录大区中一个公开账号和一个在 WeGame 中确认隐藏的账号：
先确认客户端状态，再查询两者近期战绩并用本人可见记录核对 game_id、英雄和胜负。
另检查客户端重启、未登录、错误 token、无效昵称及有效空页。
未取得隐藏账号的真实非空记录前，不宣称隐藏战绩能力已验收。
