# Akashic Agent 插件化实施交接计划

- 编写日期：2026-09-23。
- 分析仓库：`/Users/namei/idea/akashic`。
- 分析基线：`4cc5cbe31523d6d3fe8c59f07a53c97c729f57cb`。
- 本文件状态：实施计划；编写本计划时只读取源码和测试，未执行本文测试，也未实施架构修改。
- 目标：个人 Agent 的业务能力和业务存储可通过正式插件安装、组合和替换；基础内核负责通用运行机制。
- 更新策略：保留当前完整 Root 换代，不同时实现 Cordis 式局部动态重绑定。

本文中的拟新增文件名是建议落点。已有文件的链接指向分析仓库；接手者在其他 worktree 中应使用对应路径。

## 1. 可直接交给执行模型的任务

请先阅读本计划，再检查实际 HEAD、工作区改动和适用的 AGENTS.md。按照 P0～P7 顺序实施插件化，保留现有业务结果、持久化语义和整体换代协议。每个阶段需要代码、针对性验证及可恢复的阶段记录，不能只移动目录或补文档。

在隔离 worktree 和一次性 workspace 中开发测试。不要修改正在运行的个人 Agent 数据、已安装插件缓存和现有凭据；本任务不包含部署、正式数据迁移或向真实用户发送测试消息。独立测试目录和受控本地 HTTP/MCP 服务属于实施范围。

先完成当前版本的测试基线，再逐步提取持久化合同、迁移生产 SQLite 提供者、改造正式与候选环境装配、迁移宿主消费者、完成其他业务残留清理，最后执行独立分发和故障恢复验收。基础内核的 Task、Scope、Effect、装配依赖与发布控制保留。

不要因为遇到普通接口选择或文件组织问题反复请求确认；根据本计划与实际代码作出决定并记录。遇到会改变原始数据格式、外部效果语义或完整 Root 更新策略的需求，应先给出具体影响及替代方案。外部依赖不足时继续完成独立工作，将相应验证记为 NOT RUN，不虚构通过结果。

交付时逐项说明已完成范围、剩余范围、测试结果和证据路径。只有全部强制验收通过才能宣称本计划范围内的插件化完成。

## 2. 架构边界与范围

### 2.1 保留在基础内核

| 职责 | 保留内容 | 边界 |
|---|---|---|
| 加载与装配 | 制品选择、模块加载、Context、ServiceKey、依赖解析、固定配置输入 | 不解释模型、记忆、工具或渠道的业务配置 |
| 生命周期 | Scope、Effect、Task、通用进程资源归属、取消与排空 | 何时回复、何时调用工具、如何恢复业务效果由插件决定 |
| 运行身份 | owner、Root 身份、租约、接纳许可、宿主凭据边界 | 同进程可信插件机制不包装成安全沙箱 |
| 发布与恢复 | 完整组合选择、stable、更新操作所有权、归档、更新 journal | 业务数据库格式与迁移由对应提供者负责 |
| 宿主基础 | 进程启动退出、通用传输、运行环境、通用诊断 | 业务 RPC 处理、会话查询和推送策略由能力提供者承担 |

stable、归档和更新 journal 可以继续持久化。内核能在业务存储不可用时加载、诊断并选择可启动的组合，不依赖先打开 sessions.db 才能决定加载哪个存储插件。

### 2.2 本次必须完成

1. 生产消息存储实现由普通插件提供，Core 不再直接构造 MessageLog。
2. 消息、owner_records、bindings 与附件引用保持现有共同事务及引用约束。
3. 同一 sessions.db 中的其他活跃存储连接也由明确的插件负责，移除 CoreRuntime / ValidationHost 对具体存储类的持有和关闭。
4. 正式环境与隔离验证环境共用插件装配路径，各自拥有独立实例和资源。
5. 宿主查询、流式订阅、工具、记忆、投递等消费者通过公开合同使用存储。
6. 默认个人 Agent 组合从独立制品安装后通过完整功能验收；盘点所有第一方业务插件，不能用一个演示插件代表全量完成。
7. 对仍留在 Core 制品中的业务实现逐项迁出或说明确属通用内核机制；仅修改角色标签不算完成。

### 2.3 本次不扩展

不新增 Redis/Kafka/RabbitMQ，不改成多进程分布式事务，不以此任务引入新的模型、记忆算法、数据库产品或局部 HMR。不更换现有持久化数据格式，不删除或重新编码历史消息，不把外部工具副作用纳入 SQLite 事务承诺。

## 3. 当前代码地图

| 当前入口 | 事实与迁移重点 |
|---|---|
| [bootstrap/tools.py](/Users/namei/idea/akashic/bootstrap/tools.py) | CoreRuntime 持有消息、附件、接纳、身份、输入交接存储；build_core_runtime 直接创建连接 |
| [agent/plugins/manager.py](/Users/namei/idea/akashic/agent/plugins/manager.py) | 注入消息相关服务；建立验证环境时再次创建具体存储；保留其通用发布职责 |
| [agent/plugins/validation.py](/Users/namei/idea/akashic/agent/plugins/validation.py) | ValidationHost 直接持有、使用、关闭业务存储；需要改成环境与作用域管理 |
| [agent/plugin_composition/messages.py](/Users/namei/idea/akashic/agent/plugin_composition/messages.py) | 直接转导出 session.log 的实现类；服务接口尚未完全脱离具体实现 |
| [agent/plugin_composition/bindings.py](/Users/namei/idea/akashic/agent/plugin_composition/bindings.py) | 同时涉及运行组合绑定与 MessageLog 存储；分离存储合同和运行选择机制 |
| [session/log.py](/Users/namei/idea/akashic/session/log.py) | 消息编号、来源 head 校验、writer 失效、owner CAS、同库事务及唯一工具结果 |
| [session/embedding_store.py](/Users/namei/idea/akashic/session/embedding_store.py) | 消息 embedding 记录直接依赖 MessageLog；属于必须处理的隐藏消费者 |
| [session/artifact_store.py](/Users/namei/idea/akashic/session/artifact_store.py) | 附件 metadata、导入记录与消息引用关系 |
| [session/admissions.py](/Users/namei/idea/akashic/session/admissions.py)、[session/identities.py](/Users/namei/idea/akashic/session/identities.py)、[session/inbound_store.py](/Users/namei/idea/akashic/session/inbound_store.py) | 同库接纳、渠道身份和输入交接存储；不能拆出后遗漏恢复路径 |
| [bootstrap/app_server.py](/Users/namei/idea/akashic/bootstrap/app_server.py)、[agent/control/service.py](/Users/namei/idea/akashic/agent/control/service.py) | 持有具体消息目录及订阅；换代后不能继续使用旧连接 |
| [plugins/tools/execution.py](/Users/namei/idea/akashic/plugins/tools/execution.py) | 工具结果与回执的共同事务，是迁移的关键业务验收点 |
| [scripts/build_plugin_distribution.py](/Users/namei/idea/akashic/scripts/build_plugin_distribution.py) | 当前 Core 路径清单包含整个 session / infra 等目录，需重新核对制品边界 |
| [docker/host-runtime/profiles/default.json](/Users/namei/idea/akashic/docker/host-runtime/profiles/default.json) | 默认组合必须显式安装和依赖生产存储插件 |

同时盘点 bus、附件物理存储、初始化脚本、迁移 runner、SDK、Web/Mobile 与外部插件的实际消费者。清单需要区分生产调用、测试调用、历史迁移和退役兼容代码。

参考决定：[0065](/Users/namei/idea/akashic/docs/decisions/0065-plugin-boundary-checks-do-not-grant-core-ownership.md)、[0070](/Users/namei/idea/akashic/docs/decisions/0070-plugins-own-persisted-data.md)、[0071](/Users/namei/idea/akashic/docs/decisions/0071-plugin-composition-and-whole-runtime-updates.md)。

## 4. 不得丢失的行为合同

- 已提交消息正常运行时只追加；message_id、session、source、seq、CallRef、metadata 和历史编码语义保持兼容。
- expected_source_head 冲突必须显式拒绝；不能通过自动覆盖、无条件重试或去掉校验获得绿色测试。
- 过期 writer 禁止新增提交；已存在的相同幂等消息读取行为遵循现有合同。
- owner_records 的业务内容归插件解释；owner 隔离、version CAS 与事务原子性由存储能力保证。
- 同一 ToolCall 只能有一个最终 ToolResult；结果和 done 回执一起提交，done 不等于业务成功。
- 已启动但结果未知的工具先核查真实状态，只有符合既有幂等和授权条件才允许重试。
- 存储 v1 保持当前同步 API 与不跨 await 的事务回调。不能简单改成异步写入，破坏来源准入中“检查、写入、撤权”的顺序。
- 消息订阅在持久提交后通知；after_seq 支持补读已提交消息，不承诺重放断线期间未落库的流式草稿。
- 发布后服务绑定固定；完整 Root 换代仍先停止接纳、排空旧工作、关闭旧组合，再建立新正式组合。
- 取消和超时撤销本次更新的提交权；迟到成功不能恢复提交权。关闭失败保留真实 owner 和可重试句柄。
- 代码组合回退不等于业务数据回退；不得通过恢复 stable 指针宣称数据库或外部效果已回滚。

## 5. 分阶段实施

### P0：建立可比较基线

操作：

1. 记录实际 HEAD、工作区状态、运行时版本；创建隔离 worktree，保留用户改动。若本计划尚未提交，单独将本文件带入 worktree。
2. 建立 `pluginization-inventory.json`：每个能力的当前 owner、消费者、服务 key、数据文件、安装制品、目标 owner 与验收用例。
3. 运行第 7 节的现有关键测试并保存 JUnit / 日志，分别记录既有失败和本次引入的失败。
4. 用合成数据建立旧版数据库样本，覆盖消息、工具调用/结果、owner_records、bindings、附件、接纳和输入交接；保存逻辑导出用于迁移后比较。
5. 固定性能样本与运行方法：消息读取、常用追加、取消、启动、完整换代、关闭后资源数量。先测基线，再写可接受预算。

完成标准：基线可复现；关键链路既有失败已修复或明确定位。与本改造无关的失败可以登记后继续，不得改成 skip 伪装通过；受影响链路的失败必须在迁移前解决。

### P1：提取最小持久化合同

建议新增 `agent/plugin_contracts/persistence.py`，或将其拆成该包内少量纯合同模块。只包含 Protocol、稳定值类型和错误，不包含 SQL、文件操作或插件实现。复用现有消息值类型，避免因重复定义导致 isinstance / 编码身份变化。

合同至少覆盖实际消费者需要的：MessageReader、MessageWriter、MessageCatalog、OwnerStore、OwnerTransaction、会话属性、分页结果、binding records、embedding records。附件、身份及输入交接按实际职责拥有各自窄合同，不能暴露通用 SQL 执行器。

关键动作：

1. 用生产消费者倒推接口，禁止把 MessageLog 全部私有方法复制成公共 API。
2. 消费者不再依赖实现类、私有连接或具体类型判断；稳定服务 key 优先保留，字符串中已有 core 前缀不作为实现归属依据。
3. 同一 provider 内完成 transaction.append + transaction.save；跨不同存储实例或失效授权必须拒绝。
4. 将持久 binding 读写与“在本次 Root 选择实际服务”的逻辑分离。前者由存储提供，后者可以保留为内核机制。
5. 更新公开模块清单与精确 import 欠账，不能放宽检查器或扩大整个目录豁免。

验证：原实现先通过新合同调用；运行消息、owner CAS、工具回执和类型检查。必要的旧导入兼容仅用于已确认消费者，记录移除条件，不得保留 Core 自动选择旧实现的兜底。

完成标准：业务消费者只依赖合同；本阶段仍使用旧存储时业务结果不变。

### P2：实现生产 SQLite 存储插件

建议新增 `plugins/persistence_sqlite/`，以普通 `apply(ctx)` 注册存储服务。迁移活跃实现及其生命周期，不能通过该插件转调用 Core 中原来的 MessageLog 完成最终交付。

实施顺序：

1. 先迁移消息、owner_records、bindings、消息 embedding 与附件引用的存储实现。
2. 随后迁移同一 sessions.db 中活跃的附件 metadata、身份、接纳、输入交接连接。初版允许一个 SQLite provider 对外提供多个窄服务，避免人为拆散数据库一致性边界。
3. SQLite provider 只实现记录与事务机制，工具恢复、投递策略、来源控制等算法留在业务插件。
4. 正式路径仍使用现有 workspace/sessions.db，通过声明过的 workspace_file 获取；不要在迁移架构时顺便移动正式数据或重建 ID。
5. 数据库创建、现有 schema 验证与显式迁移由提供者及维护入口负责。审计 initialize_empty_workspace、安装脚本和 yoyo runner，解除 Core 启动必须先初始化业务库的依赖。
6. 已发布迁移保持不可变；历史迁移确需旧导入时，使用明确的迁移支持包并验证可执行，不能让正常运行依赖隐藏旧实现。
7. 所有连接通过 Effect/Scope 登记关闭；部分初始化失败关闭已取得资源，关闭失败保留 owner。

验证：新插件读写旧样本；比较逻辑状态、原始消息编码、编号和引用。基于同一事务注入回执保存失败，证明结果消息回滚、started 记录保留、恢复查询不重复调用。

完成标准：生产存储实现位于独立插件制品；使用旧数据无需破坏性迁移；所有相关共同事务保持有效。

### P3：重接正式启动、候选验证和恢复

修改 bootstrap、PluginManager、ValidationHost：宿主建立通用运行环境，再通过依赖图装配存储及业务插件。CoreRuntime 不再拥有 MessageLog 等具体字段和业务数据库关闭列表。

必须解决的事项：

1. 避免启动环：安装选择、制品解析和更新控制面可先于业务存储启动；存储 provider 初始化不能先要求创建依赖它自身的持久 binding。
2. 正式 Root：提供者 ready 后消费者才能启动；全部必需服务就绪后才开放输入。
3. 仅做候选装配检查的 Root：保留当前执行限制，禁止取得正式写入权；不能因迁移而偷偷运行正式业务。
4. 隔离行为验证 Root：使用自己 workspace、数据库、输入 owner 与 Task/进程资源；不借用旧 Root 的存储实例。
5. 当前候选从独立目录建立的语义保留；需要历史样本时由测试/数据 owner 显式准备一致副本，不添加通用全目录复制。
6. 真正宿主启动后的 stale admission 清理由明确启动恢复流程控制；不能在每次 provider apply 或热更新时无条件清理其他工作的接纳记录。
7. 旧组合关闭失败不得抢先创建冲突的新资源；失败恢复使用原选择重新建立真实实例。

验证：空组合启动；缺必需存储时聊天入口明确不可用；正式与验证数据库实例隔离；初始化、关闭失败及恢复测试。

完成标准：正式与验证装配不再硬编码数据库实现；仍保留完整 Root 换代和控制面的独立恢复能力。

### P4：迁移所有消费者并处理长期订阅

重点检查会话 HTTP/RPC、WebSocket、Mobile、工具、Akasha、compaction、Markdown memory、投递、调度、Wake、附件和输入恢复。

1. 请求通过当前 Root 的服务和租约取得 reader/writer；不在宿主单例长期缓存已被替换的存储对象。
2. 同一请求的校验、读取和提交使用同一选定实例；Task 继续登记 writer 撤销动作。
3. 长期消息订阅必须有退出换代的路径，不能永久持有租约，使完整 Root 永远无法排空。
4. 在暂停接纳时结束本代订阅或通过明确可取消作用域释放；保存最后已交付 seq，新组合 ready 后补读。是否保留传输连接根据现有协议实现，不凭空承诺不重连。
5. 只保证已提交消息补读；实时思考/文本草稿继续遵守现有临时预览语义。
6. 业务 RPC 处理可继续迁入对应插件；通用传输只负责路由和错误传播。UI 上“已结束/已取消/已送达”必须对应真实事实。

验证：长连接存在时更新能完成；消息无遗漏、无重复持久提交；旧 writer 被拒绝；客户端恢复后可查询历史；进程重启后已完成工具结果可复用。

完成标准：仓库所有生产消费者均已迁移，剩余旧导入按历史迁移/兼容需求逐项说明；不能仅迁移测试常用路径。

### P5：收拢其他业务残留与独立分发

1. 对 P0 清单逐项核对：模型、ReAct、工具/MCP、记忆、来源、投递、调度、Wake、UI 和渠道均从正式制品加载和执行。
2. 渠道输入交接算法、附件物理导入、业务查询等仍留宿主的部分迁到明确 provider；允许保留来源中立的宿主输入队列和文件传输原语，但不能保留中央业务名称分支。
3. 修改 Core 制品路径清单，排除已迁出的实现；不能只删除 plugins/ 就认为 Core 不含业务代码。
4. 更新默认 profile、初始化、独立安装、宿主镜像入口与升级维护流程。安装、初始化和首次加载顺序必须在空环境执行成功。
5. 第一方插件使用与外部插件相同的正式安装链；无源码 PYTHONPATH、checkout 路径或旧 Core 实现兜底。
6. shared API 包可以随分发提供，但只能包含合同；业务协作合同不全部塞进内核。

完成标准：仅 Core 制品可启动管理入口；安装默认组合后个人 Agent 可用；空环境不借原仓库运行。逐个插件提供实际能力调用证据，合法子集与默认组合分别测试。

### P6：证明可替换和异常情况下仍然正确

执行第 6 节测试矩阵，并运行正式安装制品的完整业务链。

替换证据分两层，不能混报：

- 通用装配替换：使用不同包名的独立提供者替换同一服务，消费者不变，原包不在运行环境。
- 持久化替换：同一消费者与同一组存储合同测试，分别使用生产 provider 和独立参考 provider。参考 provider 不调用生产 MessageLog 实现；需要声称支持持久恢复的测试必须使用真正落盘的参考实现。仅内存 fake 通过不能证明持久化可替换。

建议参考 provider 先实现冻结的最小公开合同并逐步补全，使用标准库 SQLite 即可，不要求支持新数据库产品。只通过部分合同必须记录覆盖范围，不能宣称整个持久化服务可替换。

故障测试使用事件屏障固定时序；进程死亡使用独立子进程在指定节点退出，并由另一新进程打开同一测试数据检查恢复。外部副作用用独立受控计数服务/记录器核验，不能只检查被杀进程的内存计数。

完成标准：矩阵强制项 PASS；未执行真实服务验证单独标记；无隐含旧实现委托与跨环境写入。

### P7：全量回归、报告与收尾

1. 运行受影响测试、全部 Python 回归、Web 测试、类型检查、生成协议检查和按变更选择的 Gate。
2. 用相同脚本比较改造前后业务结果及性能，报告原始测量和运行条件。性能预算在 P0 固定；不在看到结果后改阈值。
3. 删除仅为迁移存在的旁路和旧生产实现；仍需保留的迁移支持代码从普通运行链隔离。
4. 更新架构、持久状态图、插件开发说明和默认组合文档。
5. 交付 commit 列表、制品摘要、测试报告、已知限制及恢复步骤。提交代码不包含凭据、真实聊天记录或本机运行数据库。

完成标准：第 9 节逐项可核验；没有必需项用“已有单元测试/理论可行/历史报告通过”代替本次结果。

## 6. 强制测试矩阵

| 编号 | 测试 | 通过标准 |
|---|---|---|
| K01 | 缺依赖、重复 provider、依赖环 | 装配明确失败，不产生可接纳工作的半成品组合 |
| K02 | 取消、重入关闭、并发关闭 | 写入权及时撤销；等待同一次清理；失败句柄仍有 owner |
| K03 | 消费者关闭失败 | 保留其收尾依赖的 provider，不提前关闭数据库 |
| P01 | 旧样本读写和新进程重启 | 原始消息及逻辑状态一致，编号与 CallRef 不变 |
| P02 | 并发追加、source head CAS、owner CAS | 无重复编号；冲突明确；不同 owner 不越权 |
| P03 | ToolResult 写入后、done 回执保存前失败 | 共同事务回滚；恢复查询状态；不重复执行未知效果 |
| P04 | ACK 丢失后重试 | 相同身份读取既有结果，不能产生第二份消息或效果 |
| P05 | 附件与 binding 不完整 | 提交拒绝，消息和编号分配按现有事务规则回滚 |
| P06 | 两个生产实例争用同一 workspace | 保持现有宿主独占约束，不能同时接纳重复工作 |
| R01 | 候选初始化失败/验证被取消 | 正式数据和 stable 不变；候选清理结果可核查 |
| R02 | 换代期间有活动 Task 与长订阅 | 旧工作按合同退出；订阅释放；新工作只使用新组合 |
| R03 | 排空超时、释放资源失败、新实例启动失败 | 不发布伪成功；只在实际允许条件下恢复旧组合 |
| R04 | stable 提交前/后强杀 | 重启分别使用未变的旧选择/已经提交的新选择；不续跑未经授权候选 |
| R05 | 工具效果已发生但结果未提交时强杀 | 重启核查效果；无法核实则明确未知，不盲目重跑 |
| R06 | 数据库事务中强杀 | 新进程看见完整已提交状态，不出现半条工具结算 |
| D01 | 无业务源码的 Core 制品空载 | 管理功能可用，缺失业务能力明确报告 |
| D02 | 独立安装全部第一方插件 | 实际执行公开能力，记录输出或效果及对应持久证据 |
| D03 | 移除原 provider、只改装配后替换 | 消费者代码不改，独立实现通过同一合同 |
| D04 | 故意提供只注册不执行的假能力 | 验收失败，证明测试检查真实功能而非目录存在 |
| E01 | 输入→模型→工具→结果→回复→投递→重启读回 | 正确业务输出、消息、执行回执和投递回执一致 |
| E02 | 跨会话记忆与 feedback | 提交后学习、只读检索、定向反馈和重建保持既有合同 |
| E03 | 长 Shell、取消与增量输出 | 增量读取正确；取消/结束后的进程与句柄按合同处理 |
| E04 | 调度和主动反馈 | 触发、执行、投递、状态结算与后续回应关联各自可核查 |
| E05 | 更新前后客户端连续会话 | 已提交消息可补读，过期提交拒绝，工具结果不丢失 |

清理成功后检查本测试创建的 task、子进程、监听器、连接和端口是否释放。清理失败用例要求保留可诊断 owner，不能强行把计数置零充当成功。对关键并发顺序使用确定性同步，并补有界重复运行；大量 sleep 不作为竞态覆盖方法。

## 7. 复用测试和命令入口

已有测试是迁移起点，文件存在不代表当前提交已通过。按当前实现调整 fixture 和公共接口，不能删除业务断言、添加无依据 skip 或将异常吞掉。

| 测试组 | 现有位置 |
|---|---|
| 消息、工具共同事务 | [test_message_log.py](/Users/namei/idea/akashic/tests/test_message_log.py)、[test_tool_execution_receipts.py](/Users/namei/idea/akashic/tests/test_tool_execution_receipts.py) |
| 来源取消、Task 与消息服务 | [test_task_scopes.py](/Users/namei/idea/akashic/tests/test_task_scopes.py)、[test_conversation_source.py](/Users/namei/idea/akashic/tests/test_conversation_source.py)、[test_message_services.py](/Users/namei/idea/akashic/tests/test_message_services.py) |
| 关闭和完整 Root | [test_plugin_composition_lifecycle.py](/Users/namei/idea/akashic/tests/test_plugin_composition_lifecycle.py)、[test_plugin_fresh_root.py](/Users/namei/idea/akashic/tests/test_plugin_fresh_root.py)、[test_plugin_uninstall_root_drain.py](/Users/namei/idea/akashic/tests/test_plugin_uninstall_root_drain.py) |
| 更新授权和操作生命周期 | [test_plugin_update_operation_lease.py](/Users/namei/idea/akashic/tests/test_plugin_update_operation_lease.py)、[test_plugin_publication_admission.py](/Users/namei/idea/akashic/tests/test_plugin_publication_admission.py) |
| 独立安装和替换 | [test_plugin_distribution.py](/Users/namei/idea/akashic/tests/test_plugin_distribution.py)、[test_plugin_external_acceptance.py](/Users/namei/idea/akashic/tests/test_plugin_external_acceptance.py) |
| 完整回复与投递 | [test_installed_reply_delivery.py](/Users/namei/idea/akashic/tests/test_installed_reply_delivery.py) |

以下命令在实施 worktree 的仓库根执行；先按现有 requirements 和 CI 安装依赖。基线 SHA 应固定为实际实施基线；默认采用本文件记录的提交，不用 HEAD 冒充差异基线。

```bash
PLUGINIZATION_BASE=4cc5cbe31523d6d3fe8c59f07a53c97c729f57cb
.venv/bin/python scripts/plugin_boundary.py check --base "$PLUGINIZATION_BASE"
.venv/bin/python scripts/plugin_inventory.py --strict
.venv/bin/pyright --level error
.venv/bin/pyright --project pyrightconfig.tests.json --level error
.venv/bin/pytest -q tests/test_message_log.py tests/test_tool_execution_receipts.py tests/test_task_scopes.py tests/test_conversation_source.py tests/test_message_services.py
.venv/bin/pytest -q tests/test_plugin_composition_lifecycle.py tests/test_plugin_fresh_root.py tests/test_plugin_uninstall_root_drain.py tests/test_plugin_update_operation_lease.py tests/test_plugin_publication_admission.py
.venv/bin/pytest -q tests/test_plugin_distribution.py tests/test_plugin_external_acceptance.py tests/test_installed_reply_delivery.py
```

受影响链路通过后再执行完整 CI 所需检查，参考 [.github/workflows/ci.yml](/Users/namei/idea/akashic/.github/workflows/ci.yml)。每组测试保存命令、退出码、JUnit 和日志；给外部服务和子进程测试设置有界截止时间，超时记失败并清理所属测试资源。

独立分发入口复用：

- [build_plugin_distribution.py](/Users/namei/idea/akashic/scripts/build_plugin_distribution.py)：从明确 commit 构建制品，不能漏掉只存在工作区的改动。
- [install_plugin_distribution.py](/Users/namei/idea/akashic/scripts/install_plugin_distribution.py)：安装到新建的测试 workspace / plugins-home。
- [plugin_external_acceptance.py](/Users/namei/idea/akashic/docker/debug/plugin_external_acceptance.py)：使用 distribution、core-tar/core-root、capability-calls-json、business-composition-json 和 require-capabilities 验证真实能力。

必须补全 capability oracle 与合法组合清单，不能仅运行验收脚本自身的单元测试就宣称默认分发已验证。运行容器/独立进程不得挂载原 checkout 作为业务模块来源。执行前读取这些脚本的当前 --help，避免照抄旧参数。

建议新增测试模块：`tests/contracts/test_persistence_contract.py`、`tests/test_persistence_plugin.py`、`tests/test_storage_subscription_reload.py`、`tests/test_pluginization_crash_recovery.py`。名称可调整，但矩阵覆盖不得省略。

## 8. 分阶段报告和真实模型验证

每个阶段输出一份报告，至少包括：阶段目标、修改 commit、涉及服务和数据 owner、已通过/失败/未执行测试、下一阶段输入、已知风险和恢复方式。报告统一保留机器可读结果和简短说明。

推荐状态只用 PASS、FAIL、NOT RUN、BLOCKED；BLOCKED 需说明缺少的外部条件。报告还应区分：实现已存在、单元合同通过、正式安装通过、受控协议端到端通过、真实外部服务通过。

固定输入的模型/渠道 HTTP 服务用于架构和业务链回归，内部生产插件不替换成返回成功的 mock。真实模型验证另做少量冒烟：工具调用、跨会话记忆、长 Shell 和一次完整更新后继续对话。需要费用或外部写入时遵守任务已有授权；缺条件记 NOT RUN，不影响已独立取得的架构证据。

Terminal-Bench 等任务分数、记忆问答准确率可以另做能力评估，不作为接口隔离、事务原子性或插件可替换性的替代证据。

## 9. 完成定义与最终交付

全部满足后才将本次改造标记为完成：

- [ ] 所有第一方业务能力都有当前 owner、公开合同和实际安装调用证据。
- [ ] Core 生产路径不构造或兜底调用已迁出的业务存储实现。
- [ ] 空 Core、默认组合、合法子集与独立替换均有独立制品运行证据。
- [ ] 消息、owner CAS、工具结果、bindings、附件和输入交接语义保持兼容。
- [ ] 正式/候选环境隔离、取消、订阅换代、关闭失败及进程强杀恢复通过。
- [ ] 默认个人 Agent 的对话、工具、记忆、调度/反馈功能有逐项结果，未执行项明确列出。
- [ ] 必需自动化回归和类型检查通过；既有无关失败单独列出，不混入通过数量。
- [ ] 性能和资源测量使用固定基线，达到预先约定预算或明确披露未解决回归。
- [ ] 文档描述当前实现；历史验收数字没有冒充本次执行结果。
- [ ] 交付可恢复的 commit、制品摘要、报告路径、已知限制和下一步。

如果仅完成部分阶段，交付准确的阶段结果，保留未完成清单。当前源文档 [plugin-whole-runtime-simplification.md](/Users/namei/idea/akashic/docs/design/plugin-whole-runtime-simplification.md:3) 明确记录运行验收未执行，不能直接沿用历史回归数量宣称本次运行已验证。
