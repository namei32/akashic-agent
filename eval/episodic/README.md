# EpBench 情景恢复与 InMind 间接应用

这套入口比较 Hybrid（向量、BM25、RRF）与当前 Akasha 的真实 `MemoryCycle` 和原生读出。它复用已冻结的实验框架，不改生产记忆参数、消息库、插件配置或既有实验结果。

共享检索和向量缓存来自评测提交 `4a172d3e339ff806b5b2641426fe50e78c0130c5`。`--harness-root` 指向包含该提交 `eval/association` 的 checkout；装载前逐文件核对 Git 内容，只扩展 `eval` 包路径，生产算法必须来自当前 checkout。正式调用还需要既有、已验证的私有 stdio provider 目录；本入口不会自动连接日常 Agent 或查找用户凭据。

## 数据与协议

- [EpBench 源码](https://github.com/ahstat/episodic-memory-benchmark)固定到 `892b22af097d4389d4f1b9cd47b5c51fdacd9bef`；[已发布语料](https://doi.org/10.6084/m9.figshare.28244480)包含 `book.json`、`df_book_groundtruth.parquet` 和 `df_qa.parquet`。逐章节原文摄入，答案、事件表、线索模板和证据标签仅用于评测，不参与建图。`pyarrow` 只在 prepare 阶段需要，使用私有环境安装，不改共享开发环境。
- [InMind](https://github.com/imlrz/InMind)固定到 `5a2ab2686d3d5b832575f0288d41ecc93eab358b`。保留 47 个背景会话、486 条背景消息；目标问答追加到第九会话末尾，之前有 41 条背景用户消息，之后还有 38 个完整会话。每个任务独立建图；直接和间接查询读取同一冻结状态，测试回答不进入学习。
- 摄入使用明确标记的顺序时钟，事件日期保留在原文中。EpBench 是章节观察适配，InMind 是对话组件适配；不宣称验证了生产 Agent 生命周期或自然遗忘。
- 固定 InMind 背景的原始时间标签存在倒退，不能直接作为单调学习时钟。保留作者规定的会话顺序和原文日期，不为了得到单调时间而重排背景。
- InMind 按领域、固定种子划分完整任务；dev/holdout 两个查询成对保留。开发 pilot 按领域轮换抽取。`--split all` 包含开发曝光任务，应明确披露。
- EpBench 按整本书指定 dev/holdout，开发 pilot 按线索、题型和事件数量分层。短篇和长篇可以共享事件，不能把它们称为未见用户或独立世界。

## 准备

从项目根目录执行。输出文件和运行目录须是新路径：

```sh
python -m eval.episodic prepare --inmind-root /data/InMind --out /experiment/inmind-suite.json
python -m eval.episodic prepare --epbench-source /data/episodic-memory-benchmark \
  --dev-book /data/short-book --holdout-book /data/long-book --out /experiment/epbench-suite.json
```

每套 suite 固定原始数据、官方提示词与评分源码的摘要。运行前再次核对所有文件；缺失、改写、证据不在历史、章节不对应或背景协议变化均明确失败。

## 运行

所有阶段使用同一 settings JSON。默认沿用旧实验的 Sol/max、1024 维 embedding、12,000 字符证据上限、每题三次；这是内部对照配置，不是 InMind 官方 gpt-5-mini 榜单配置。实际输入量与 usage 单独保存，输出 token 参数不等于当前 provider 强制上限。

```sh
python -m eval.episodic embed --suite /experiment/suite.json --settings /experiment/settings.json \
  --harness-root /source/frozen-evaluation --cache /experiment/vectors.db --split dev --limit 6 \
  --provider-root /experiment/private-provider --config /experiment/model.toml \
  --model-workspace /experiment/embed-workspace --out /experiment/embedding
python -m eval.episodic retrieve --suite /experiment/suite.json --settings /experiment/settings.json \
  --harness-root /source/frozen-evaluation --cache /experiment/vectors.db --split dev --limit 6 \
  --out /experiment/retrieval
python -m eval.episodic answer --suite /experiment/suite.json --settings /experiment/settings.json \
  --harness-root /source/frozen-evaluation --retrieval /experiment/retrieval --out /experiment/answer-requests
```

`answer` 和 `judge` 默认生成完整请求用于审阅，不调用模型。实际执行时使用另一新输出目录，加 `--send` 及 `--provider-root`、`--config`、`--model-workspace`。`embed` 明确执行 embedding 调用。正式 holdout 不接受临时 `--limit`；需要小批次时先冻结实际题目清单。

```sh
python -m eval.episodic judge --suite /experiment/suite.json --settings /experiment/settings.json \
  --harness-root /source/frozen-evaluation --answers /experiment/answers --out /experiment/judge-requests
python -m eval.episodic report --suite /experiment/suite.json --settings /experiment/settings.json \
  --harness-root /source/frozen-evaluation --retrieval /experiment/retrieval \
  --judgments /experiment/judgments --out /experiment/report
```

EpBench 每道题、每轮需要 H/F 两次回答和两次评分。InMind 每个任务、每轮需要直接 H/F、间接 H/F、间接 oracle 共五次回答及五次答案评分；两个 H/F target-recall 评分只需各一次。oracle 仅显式提供目标记忆，作为世界知识桥接对照，不参与 H/F 总分。

## 评分与恢复

EpBench 加载已核对摘要的官方纯提示词和 `generate_metric`，记录 lenient/harsh F1；当前不执行 chronological ordering score，答案 F1 不证明顺序正确。InMind 分开报告直接回忆、间接目标召回、间接应用和 oracle 应用；target-recall 裁判不接收模型答案。

`rescore --judgments /experiment/old-judgments --out /experiment/new-rescore` 用当前解析器本地重算全部已保存原始裁判输出，保留源文件和旧错误，不调用模型。官方 EpBench 公式用 `float(value)`，所以有限的数字字符串与 JSON 数字均可解析；引用身份、顺序、数量与分数范围仍严格检查。

每次外部调用前先 fsync 调用意图，返回后保存并 fsync 原始回答、用量、调用 ID 和错误，再推进。恢复只接着唯一请求前缀运行，已有失败不重新生成；只有意图而没有结果的请求记为完成情况未知的失败，不重新生成答案。超时、模型身份变化或协议失配立即停机；连续三个请求错误停机。无效裁判输出保留 pending，阻止最终质量结论；当前没有自动重新判分入口。查询核对完整图指纹，向量缓存在检索期间只读。

原始语料只读；suite 和阶段产物只新增，恢复只追加原始调用记录，不自动删除或覆盖。数据更新需要新 suite 和新运行目录，源码与数据身份改变会拒绝恢复。实验目录是恢复点；不操作日常 workspace。

报告先按题平均重复结果，再按故事或 InMind 任务聚类。单本书不给伪造的跨故事置信区间；InMind 所有任务共用一个背景，任务区间仅条件于该背景。纯检索结果、结构向量验证和待判分记录均不构成回答质量证据。

静态检查：`pyright --project eval/episodic/pyrightconfig.json --pythonpath /path/to/project-python`。按仓库 TST-001 使用真实组件与 CLI 验证，不新增函数 mock 单测。
