# Default Memory 的 LongMemEval 评测

评测使用固定发行版中的实际 Default Memory，不改变线上记忆策略。先运行覆盖六种题型及 abstention 的 20 题试验，再按实际用量安排完整运行。若根据试验题调整模型、提示词或检索参数，必须将这些题标为开发样本。

## 数据与输入边界

- 数据：LongMemEval-S cleaned，revision `98d7416c24c778c2fee6e6f3006e7a073259d48f`，500 个独立问题。
- 官方评分源码：LongMemEval commit `9e0b455f4ef0e2ab8f2e582289761153549043fc`。
- 被测发行版：`c66c552c496c0c40513d1fee9da873d40c118ce7`。记录 distribution、数据、问题及脚本摘要；换制品使用新的评测根目录。
- 抽样：按 `SHA256("akashic-lme-pilot-v1:" + question_id)` 排序，六个非 abstention 题型各取 3 题，再取 2 道 abstention。没有按预计难度或答案选题。
- 保留原始英文、角色、顺序和日期。数据没有时区，统一按 UTC 解释；同会话按消息顺序增加微秒以固定排序。
- 学习只读取 `history.json`；问题、答案及证据标签留在独立的 `qa.json`，完成学习后才加载。不同问题不共享目标相关记忆。

正式 workspace 只用于读取模型配置并创建权限受限的凭据模板。模板副本清空旧 `model_calls` 并 VACUUM，不复制原聊天、原记忆或附件。每题拥有独立 workspace、HOME、SQLite 与插件 cache。凭据、向量、数据和报告均保留在仓库外。

## 实际调用链

┌────────────────────┐    ┌─────────────────────────────┐
│ 无答案标签的原始历史 │───▶│ 新建 Ledger 历史输入 fixture │
└────────────────────┘    └──────────────┬──────────────┘
                                        ▼
                          ┌─────────────────────────────┐
                          │ 已安装插件 consume → 提炼库 │
                          └──────────────┬──────────────┘
                                        ▼
                          ┌─────────────────────────────┐
                          │ 冻结 → recall → 最终上下文 │
                          └──────────────┬──────────────┘
                                        ▼
                          ┌─────────────────────────────┐
                          │ 独立回答 → 官方提示词评分  │
                          └─────────────────────────────┘

Ledger 正常 writer 使用接纳时间，没有历史时间导入参数。runner 先由正式 Ledger 初始化空库，关闭 runtime 后仅向空 fixture 插入原始历史，再冷启动。这是 benchmark 输入准备，不是线上导入能力，不更改已有消息时间。非空 fixture 拒绝重复初始化。学习、来源资格、提炼、事务与召回均走已安装插件的 `DEFAULT_MEMORY` 公共入口，不使用模型 mock。

`consume(limit=1)` 每次处理一个完整轮次；先等本轮完成才前进。失败先等模型账本的耐久退避截止时间，再通过公开接口显式重试，每个来源最多三次；仍失败的题保留错误。完成后核对学习回执的提交顺序与来源时间顺序一致。查询传入数据集 `question_date`，避免机器日期改变衰减分数。

## 对照与评分

| 组别 | 上下文来源 |
|---|---|
| Default Memory | 当前插件提炼后的有效记忆，原向量、关键词和 RRF 实现 |
| Raw hybrid | 原始消息按 1000 字符、150 字符重叠分块，BM25 与同一向量模型经 RRF 融合 |
| Oracle | 官方标注的证据消息；缺少消息标注时使用证据会话 |

Raw hybrid 固定 BM25 `k1=1.2,b=0.75`、等权 RRF `k=60`、每路前 16 候选及最终 Top-8。当前插件的子串匹配不称为 BM25。三组使用相同回答模型、提示词和输出预算，上下文同时限制 6000 字符和 2048 个 `cl100k_base` token。这是统一预算计量器，不声称等于 provider 的计费 tokenizer。原始召回与预算投影后上下文分别保存；Oracle 证据放不下时标记 `all_evidence_fits=false`。

回答使用独立执行、low reasoning 和 2048 输出 token 上限。评分采用固定官方源码中的 `get_anscheck_prompt`，不执行上游 API 主流程。首轮评分沿用当前配置的回答模型，并记录实际型号；未复现官方 GPT-4o judge 配置，不能直接与不同模型或预算下的外部成绩比较。

只有全部案例完成时显示总准确率，同时报告分题型结果。运行失败单列，不丢弃问题提高分数。每题保存实际上下文、回答、原始判分和调用 ID。证据 ID 覆盖仅帮助定位来源，不能证明提炼文本保留了正确事实。

聊天调用由真实模型账本计量。Raw hybrid 向量调用保存每批 usage，但当前发行版不持久化学习及默认召回的向量 usage，因此该部分成本明确为未知；聊天 token 合计不能称为全部费用。

## 调用方式

先在 WSL 准备仓库外的空目录。以下为参数形式，路径须换为已核对的实际位置。

```bash
python scripts/longmemeval_run.py fetch --root /path/to/evaluation
python scripts/longmemeval_run.py prepare --root /path/to/evaluation --distribution /path/to/release/distribution --credential-workspace /path/to/workspace
python scripts/longmemeval_run.py probe --root /path/to/evaluation
python scripts/longmemeval_run.py run --root /path/to/evaluation --workers 4
python scripts/longmemeval_run.py report --root /path/to/evaluation
```

`probe` 只处理首题的一个真实完整轮次，随后 run 从耐久回执继续。`prepare --full` 生成全量清单，须使用新的根目录。长任务启动前复制 harness 并保存摘要，源码后续修改不改变在途评测。每个根目录只能有一个 controller，不能同时启动两个 run。

学习进度、每批基线向量、回答和判分可恢复，已完成题不重复执行。运行结束核对历史内容不变，以及查询前后的记忆事实、来源、学习回执和元数据不变。查询回执及模型调用账允许在评测副本中追加。

## 解释结果

当前提炼器要求用户证据及长期价值，可能不保存助手独有信息和临时任务细节。仍报告这些题型的完整成绩，并区分策略覆盖范围与错误遗漏。公共 benchmark 不替代项目隔离、暂停恢复、遗忘语义和已有聊天保护验收。

来源：[官方仓库](https://github.com/xiaowu0162/LongMemEval)、[清洗版数据](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned)。

恢复失败的历史 attempt 时，使用 recover 在新根目录从空记忆重建，保留原失败证据。模型回执包含 binding 身份，不能直接跨实例搬运；实际绑定不一致的回执已由模型层拒绝，恢复运行使用新调用，不绕过该校验。

数据时间审计单独保存：输入数组不一定按时间戳排序，个别实例还含提问日期之后的会话。主运行保留官方全部输入，使用插件既有时间排序；报告单列异常，不擅自重排标签、删除案例或声称完成严格的真实时间因果测试。

答题结束后可运行 diagnose --case QUESTION_ID：分别检查全部有效记忆与实际上下文能否支持标准答案，并运行独立无记忆对照。诊断评审不看该系统的生成答案；结果与官方答题分数分开，不将来源 ID 命中或 AI 判定当作人工确认。若判断互相矛盾，标为 diagnostic_disagreement。

用户明确要求试验通过后继续全量时，pipeline --full-root FULL_DIR 会先准备 500 题，在 20 题全部完成且顺序、历史与记忆保护校验通过后启动。通过不要求答案全部正确。全量沿用相同发行版、模型配置、预算和判分方式，可复用这 20 个相同输入的已完成结果；只复制不再变化的结果文件，完整数据库证据仍保留在试验目录。500 题全部完成之前不宣称全量成功。
