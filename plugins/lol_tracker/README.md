# 英雄联盟战绩

为 Akashic 提供国服战绩查询、关注名单、每日汇总和 AI 赛后分析。Windows 采集器只对本机
英雄联盟客户端进行 GET 请求，凭据只在进程内和 curl 标准输入中使用。Akashic 在 WSL
保存采集事实、报告与 EventMail 提交回执，Wake 沿现有发送渠道投递。

## 配置

1. 安装插件制品，并将 `windows/collect.ps1`、`windows/install.ps1` 放在 Windows 上的源码目录。
2. 运行 `powershell -NoProfile -File install.ps1`，注册用户任务 `Akashic LoL Collector`。
   默认每 5 分钟采集，开机登录后恢复，关闭游戏时记录离线；不启动或登录游戏。
3. 在 Akashic 的“功能设置 → 英雄联盟战绩”填写采集目录的 WSL 路径，例如
   `/mnt/c/Users/<用户>/AppData/Local/Akashic/lol-tracker`。
4. 添加完整 Riot ID（例如 `namei#15100`）和区服代号（艾欧尼亚 `HN1`），选择每日时间并开启。
5. 开启 Wake 并选择接收对话。设置页可立即生成并推送近 7 天报告。

默认北京时间 09:00 汇总前一个自然日，以对局结束时间归属日期。也可选择发送时间之前的
24 小时。首次启用不会补发已经过去的当天定时报告；之后重启仍按日身份去重。
`lol_match_history(days=7)` 供 Agent 查询已关注玩家的战绩和数据覆盖情况。

## 分析和数据边界

- 分模式统计胜负、KDA、英雄表现；训练模式、少于 5 分钟的对局和重开单列。
- 有完整队伍数据时计算参团率与队内伤害占比；缺失数据保持未知。
- AI 分析使用 Akashic 的轻量模型角色。不从赛后数字推断具体操作、真实段位或输赢责任。
- 查询失败、客户端离线、无对局是不同状态。游戏关闭后仍可分析缓存；未覆盖区间明确标记。
- 本机客户端须登录玩家所在区服，战绩隐私和服务可用性可能限制查询。
  LCU 是[未获官方稳定性保证的客户端接口](https://developer.riotgames.com/docs/lol#league-client-api)，不是国服公共 Riot API。
- 采集器保存版本化数据缓存，不保存客户端口令；插件只写自己的 SQLite 和专用采集目录。
- 报告内容先持久化，再按固定事件身份提交 EventMail Alert。重放采用原内容和时间戳；
  “已提交 Wake”不等于已送达，最终证据在 Wake/Delivery。
- AI 请求中断或结果未知时不会自动重付；保留数值报告并注明分析失败，可显式生成新报告。

## 停用与恢复

网页关闭插件会发布关闭的采集配置；如卸载插件，同时停用 Windows 任务：
`Disable-ScheduledTask -TaskName 'Akashic LoL Collector'`。
关闭或卸载不删除战绩、报告、历史消息及投递回执。重新启用使用原数据继续。
代码恢复使用 Git；Windows 任务和 WSL 插件应使用同一版本的采集格式。
