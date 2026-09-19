# PersonaMem：个人偏好问答评测

2026-09-19：复用 LongMemEval 的 MessageLog 导入与 programmatic QA 链路。
前置安装、隔离、模型配置和运行语义见 `../longmemeval/README.md`。

```bash
export AKASHIC_EVAL_TEMPLATE=/absolute/experiment/template
export AKASHIC_PLUGIN_HOME=/absolute/experiment/plugin-home
python -m eval.personamem.run \
  --config /absolute/experiment/config.toml \
  --questions /absolute/data/questions_32k.csv \
  --contexts /absolute/data/shared_contexts_32k.jsonl \
  --workspace /absolute/experiment/runs/pm-001 \
  --workers 1 --limit 1 --resume-auto \
  --output /absolute/experiment/pm-001.json
```

当前数据版本的七个题型与旧标签别名均被接纳，不能因拼写差异静默漏题。
历史严格截取到 `end_index_in_shared_context`，后续对话不进入当前问题。
每题有独立 workspace；数据没有原始 Session 日期时，按合成时钟保留顺序，
不据此声称测试了真实日历时间的遗忘效果。

QA 为新的、禁止学习的内部 Session；重跑不删除旧消息，也不把模型答案学入用户历史。
评分输出 `accuracy`、`parsed_rate`、`errors` 与分题型统计。选择题正确率仅代表该任务形式，
不等同于开放式建议质量或实际用户满意度。

先用 `--limit 1` 验证入口，`--limit 0` 为全量。改变配置或代码时使用新实验根目录。
