# 0129 · 回复循环耗尽后由新输入续额

- 状态：accepted
- 日期：2026-10-27
- 范围：ReAct 预算投影；[SES-013](../projectneed.md#ses-013-回复循环耗尽后由新输入续额)
- 部分修订：[消息设计 §5.3](../design/0902-reviewed-v4.md)、[工具展示纠错预算](../design/plugin-tool-view.md)
- 关联：[0092](0092-model-generation-recovers-until-output.md)、[0100](0100-message-tool-calls-recover-at-turn-granularity.md)

## 问题与选择

循环步数限制可以阻止失控执行，但 failure 不关闭 Turn。旧实现保留满额计数，导致同一来源
之后的用户问题也在请求模型前立即失败。重启仍从日志重建这一状态，无法恢复对话。

维护者选择保留默认 40 次上限，让耗尽后的下一条用户输入取得新额度；运行中的插话和重启继续累计。

## 决定

1. 同来源 Control.failure 覆盖的输出已达到当前配置上限时，标记该段预算耗尽。
   依据实际 Output、failure 的 through_seq 和配置判断，不解析本地化错误文字。
2. 后续同来源新 Input 清空临时预算计数。普通运行中的 Input、未满额时的失败、pause/resume、
   其他来源的消息和进程重启都不单独续额；没有新 Input 时保持停止。
3. 只更新预算投影，不关闭 Turn、不补造 complete/quiet、不放弃工具调用。完整历史、model.facts、
   ToolCall、ToolResult 与模型调用账继续可读；已完成工具按原 CallRef 复用。
4. 耗尽提示说明可以发送新消息继续。旧版只含文字的 failure 按相同事实判定，无需改写或迁移。

提高上限只能推迟阻断；每条 Input 都清零会让运行中插话绕过限制；单独增加续额控制要求用户
学习额外操作。这里用已明确接纳的新输入恢复执行，保持原控制协议和失败含义。

## 状态、失败与回退

续额不新增持久对象。Message 仍只由原 writer 追加，数据没有原位更新、逻辑失效或物理减少。
进程重启和插件换代重新读取原日志；源数据损坏仍传播，续额不捕获存储、模型或工具错误。
本决定只涉及循环额度，不承诺精确费用上限，也不授权重发已有工具或投递效果。
代码回退保留全部新增消息和结果，但旧代码可能再次触发满额限制。

## 验收

`scripts/react_budget_recovery_workflow.py --steps 40` 使用 loopback HTTP 模型、真实 SourceSession、
ReAct、文件工具和 SQLite：暂停恢复、执行中插话、未满额时的 HTTP 400 和账本重开均保留计数；
显式 resume 与其他来源输入不续额；耗尽后的新 Input 获得完整新额度，再次耗尽后仍能回答追问。
同时比较既有消息全部字段、观察 UPDATE/DELETE 尝试、核对文件效果不重跑，并检查其他会话可完成。
同一脚本对旧源码复现新 Input 立即失败。所有数据和文件效果只进入隔离临时目录。
