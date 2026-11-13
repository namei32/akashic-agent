# 0131 · Codex 指挥使用 Windows 原生 CLI 后端

- 状态：proposed
- 日期：2026-11-01
- 关联：CAP-002、OUT-001、STA-001～STA-003、PLG-015；补充 [0128](0128-codex-notifications-use-local-gateway.md)

## 背景与选择

用户希望在 Akashic 指挥 Windows Codex，发送指令和图片、读取结果、调整或停止工作，
并已选择直接使用 Windows 原生 CLI。已有 Stop 通知只有 Codex → Akashic 的摘要。

新增普通可选 `codex_control` 插件，通过原生 CLI app-server 的 loopback WebSocket
明确新建或选择目标。保留现有账号、配置、模型与权限策略，不添加 bypass 参数。
独立 CLI 不要求启动桌面，也不接管另一个桌面后端的活动任务。

```text
┌──────────────────┐        ┌──────────────────────┐
│ Akashic 控制工具 ├───────▶│ Windows 原生 CLI     │
│ 文字、原始图片   │        │ 项目、聊天与回合     │
└────────┬─────────┘        └──────────────────────┘
         │                    登录任务监督 CLI 进程
         ▼
  jobs 完成提醒与实际结果读取
```

Windows 桌面的现有后端使用私有 stdio。最初考虑让桌面与插件共享 app-server，但这需
用户退出重开桌面并依赖当前安装代码中的内部环境变量。按用户后来选择改用独立 CLI，
因此不再需要该接入点。按键模拟、直接读写数据库和一轮一进程的无状态 exec 也未采用：
持续 app-server 能表达同一聊天的续聊、steer、中断和完成通知。

## 权限、状态与失败

用户明确新建或选择目标后，后续工具固定 thread ID；新建要求 Windows 绝对项目目录。
调整、中断绑定实际 turn ID；审批绑定当前准确 request ID，并要求用户明确决定。
不同时从独立 CLI 和桌面续接同一聊天。普通 Akashic 聊天不自动转发。
图片只读取本会话 Input 引用的实际附件，通过 Ledger 的窄读租约取得原字节。

插件只在自己的 owner_records 分区保存目标与回执，没有新表或业务迁移。按调用 seq
拒绝迟到旧选择；新建和发送前保存 started，断线、超时、丢 ACK 均不自动重投。
原消息、附件和 Windows 效果保留。当前没有回执、目标或日志的自动删除协议。

## 代价与验收

WebSocket transport 上游仍为实验性，只监听 loopback。计划任务以当前普通用户在登录
后启动并有限重试，不能保证未登录时可用。进程崩溃和源码回退不等于外部效果回滚；
重连后读取真实状态。权限策略的临时运行许可只供启动器，不传播到 Codex 子进程。

隔离原生 app-server、本地 Responses 服务、正式安装器、真实 Root/Ledger/工具 binding
验证新建、原图、续聊、steer、中断、重复调用、丢 ACK 和重启回读。
若以后必须控制桌面实时任务，再验证公开共享后端入口与多客户端语义。
操作与部署说明见 [插件 README](../../plugins/codex_control/README.md)。
