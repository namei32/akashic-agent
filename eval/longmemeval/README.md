# LongMemEval：Message Runtime 评测

2026-09-19：入口已适配 MessageLog、普通插件组合与 programmatic API。
它评测完整 Agent 回答能力；不是纯向量检索排名。

## 前置条件

- 独立安装的插件组合，包含 models、聊天与 Embedding driver、Akasha、Prompt、Context、ReAct、programmatic 等依赖。
- 空白模板包含经过验证的 `model-registry.sqlite3`、`memory/VEDA.md`、空白用户档案及插件 `config.input.json`。
- 设置 `AKASHIC_EVAL_TEMPLATE` 与独立 `AKASHIC_PLUGIN_HOME`。不得指向日常运行 workspace。
- Core 配置参考 `config.example.toml`；模型和行为设置由相应插件拥有。

```bash
export AKASHIC_EVAL_TEMPLATE=/absolute/experiment/template
export AKASHIC_PLUGIN_HOME=/absolute/experiment/plugin-home
python -m eval.longmemeval.run \
  --config /absolute/experiment/config.toml \
  --data /absolute/data/longmemeval_s_cleaned.json \
  --workspace /absolute/experiment/runs/run-001 \
  --workers 1 --limit 1 --resume-auto \
  --output /absolute/experiment/report-001.json
```

`--limit 0` 为全量；先跑一题检查费用、时延和日志。当前只能串行共享插件安装，
多实例实验应使用独立进程、workspace 与 plugin home。

## 数据与运行语义

支持数据集的六个 question_type；question_id 以 `_abs` 结束时使用无答案裁判规则。
数据版本、文件摘要和样本范围必须在实验报告中明确。

1. 历史按源日期回放到不同 Session，通过公开 MessageLog writer 保存完整原文。
2. 不修改已写入的历史。相同输入重复导入幂等，变化的输入需要新 workspace。
3. 评测时钟仅用于保存历史和问题时间。没有日期的数据使用单调合成时钟，不代表真实日期。
4. Akasha 通过生产消费者学习；QA 使用新的 programmatic Session，并明确排除长期学习。
5. 不强制调用 recall_memory，不假定所有问题都能回答；是否调用工具由普通 Agent 决定。
6. 摘要沿真实模型窗口触发，不另设手工压缩旁路；独立 QA 不保证历史先形成 Markdown profile。

每题保存预测、错误、QA Session ID、工具轨迹及耗时。重复 QA 保留原消息；
不再使用旧的 SessionManager、Loop 私有接口或删除旧 QA 消息。

## 评分

- `judge_acc`：已核验语义正确数 / 全部尝试题数。执行失败及缺失裁判留在分母。
- `judge_conditional_acc`：只在已完成裁判的有效回答上计算，供诊断。
- `judge_missing`：裁判缺失数，正式发布结果前应补评。
- F1、EM：辅助词面指标，不作为无答案问题的质量结论。
- 报告按题型拆分；`abstention` 独立成组。

实验根目录保存输入身份，代码、配置、模型、数据或超时预算变化时拒绝复用缓存。
中断导入目录先归档保存，不物理删除。合成冒烟的通过率不能当作公开 benchmark 成绩。

## 验证

```bash
python -m pytest -q tests/test_eval_message_runtime.py
```

这组测试验证原文/日期/会话边界、重复导入、QA 排除学习、错误分母及部分结果保留。
真实 Chat、Embedding 与安装插件链仍需独立冒烟。
