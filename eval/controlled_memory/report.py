from __future__ import annotations

from collections import Counter, defaultdict
import json
import statistics

import numpy as np

from .common import jsonl, update
from .calls import effective_judgments


def paired(rows, field, cases, *, dataset, category=None, unexposed=False):
    values = defaultdict(lambda: defaultdict(list))
    for row in rows:
        case = cases[row['case_id']]
        if case.dataset != dataset or category is not None and case.category != category:
            continue
        if unexposed and case.previously_exposed:
            continue
        if row.get(field) is not None:
            values[case.id][row['arm']].append(float(row[field]))
    complete = {i: {a: statistics.mean(x) for a, x in arms.items()} for i, arms in values.items()
                if all(a in arms for a in ('hybrid', 'full'))}
    if not complete:
        return None
    clusters = defaultdict(list)
    for i, arms in complete.items():
        clusters[cases[i].cluster_id].append(arms['full'] - arms['hybrid'])
    interval = None
    if len(clusters) >= 2:
        rng = np.random.default_rng(20261013)
        buckets = list(clusters.values())
        boot = [statistics.mean(x for index in rng.integers(len(buckets), size=len(buckets)) for x in buckets[index])
                for _ in range(2000)]
        interval = [float(x) for x in np.quantile(boot, [.025, .975])]
    means = {a: statistics.mean(v[a] for v in complete.values()) for a in ('hybrid', 'full')}
    return {**means, 'difference': means['full'] - means['hybrid'], 'interval_95': interval,
            'queries': len(complete), 'clusters': len(clusters)}


def report(root, suite, *, phase):
    directory = root / phase
    directory.mkdir(parents=True, exist_ok=True)
    retrieval = jsonl(directory / 'retrieval.jsonl')
    answers = jsonl(directory / 'answers.jsonl')
    judgments = effective_judgments(directory / 'judgments.jsonl')
    chronology = effective_judgments(directory / 'chronology.jsonl')
    scoring_attempts = [row for filename in ('judgments.jsonl', 'judgments-recovery.jsonl',
                                             'chronology.jsonl', 'chronology-recovery.jsonl')
                        for row in jsonl(directory / filename)]
    cases = {c.id: c for c in suite.cases}
    results = {'phase': phase, 'queries': len(cases), 'retrieval_rows': len(retrieval),
               'answer_rows': len(answers), 'judge_rows': len(judgments),
               'chronology_rows': len(chronology), 'retrieval': {}, 'answers': {}, 'resources': {}}
    results['complete'] = all((directory / (name + '-completion.json')).exists()
                              for name in ('retrieval', 'answers', 'judgments', 'chronology'))
    results['pending_judgments'] = sum(r.get('score') is None for r in judgments + chronology)
    results['complete'] = results['complete'] and results['pending_judgments'] == 0
    results['failed_answer_attempts'] = sum(bool(r.get('error')) for r in answers)
    results['failed_scoring_attempts'] = sum(bool(r.get('error')) and not r.get('input_error') for r in scoring_attempts)
    results['scoring_recovery_attempts'] = sum('original_request_id' in r for r in scoring_attempts)
    for dataset in sorted({c.dataset for c in suite.cases}):
        for budget in (2000, 4000, 8000):
            rows = [{**r, **r['renders'][str(budget)]} for r in retrieval if 'renders' in r]
            for metric in ('evidence_recall', 'evidence_complete'):
                results['retrieval'][f'{dataset}/{metric}@{budget}'] = paired(rows, metric, cases, dataset=dataset)
        metrics = ['longmemeval'] if dataset == 'longmemeval' else ['epbench'] if dataset == 'epbench' else ['naive', 'target-recall', 'application']
        for metric in metrics:
            rows = [r for r in judgments if r['metric'] == metric]
            results['answers'][f'{dataset}/{metric}'] = paired(rows, 'score', cases, dataset=dataset)
            if dataset == 'inmind':
                results['answers'][f'{dataset}/{metric}/previously-unexposed'] = paired(rows, 'score', cases, dataset=dataset, unexposed=True)
        for category in sorted({c.category for c in suite.cases if c.dataset == dataset}):
            metric = 'longmemeval' if dataset == 'longmemeval' else 'epbench' if dataset == 'epbench' else 'naive' if category == 'naive' else 'application'
            results['answers'][f'{dataset}/{category}'] = paired([r for r in judgments if r['metric'] == metric], 'score', cases, dataset=dataset, category=category)
    results['answers']['epbench/chronological-required-complete-tau'] = paired(chronology, 'score', cases, dataset='epbench')
    for arm in ('hybrid', 'full'):
        rs = [r for r in retrieval if r['arm'] == arm]
        usage = [r['usage'] for r in answers + scoring_attempts if r['arm'] == arm and r.get('usage')]
        known = {key: sum(u[key] for u in usage if u.get(key) is not None)
                 for key in ('input_tokens', 'output_tokens', 'cached_input_tokens', 'reasoning_output_tokens')}
        resources = {'usage_records': len(usage), 'known_usage': known}
        for key in ('context_tokens', 'selected_turn_count', 'retrieval_ms', 'build_ms', 'index_pickle_bytes'):
            numbers = [r[key] for r in rs if r.get(key) is not None]
            resources[key] = {'mean': statistics.mean(numbers), 'p95': float(np.quantile(numbers, .95))} if numbers else None
        resources['mean_presented_messages'] = statistics.mean(len(r['presented_ids']) for r in rs) if rs else None
        results['resources'][arm] = resources
    results['failures'] = [{'case_id': r['case_id'], 'arm': r['arm'], 'error': r['error']}
                           for r in answers + judgments + chronology if r.get('error')]
    pairs = defaultdict(dict)
    for row in retrieval:
        pairs[row['case_id']][row['arm']] = row
    evidence_differences = []
    for identity, arms in pairs.items():
        if set(arms) != {'hybrid', 'full'}:
            continue
        h, f = arms['hybrid'], arms['full']
        hg, fg = set(h.get('gold_hit_ids', [])), set(f.get('gold_hit_ids', []))
        evidence_differences.append({'case_id': identity, 'dataset': cases[identity].dataset,
                                    'added_gold_ids': sorted(fg - hg), 'lost_gold_ids': sorted(hg - fg),
                                    'hybrid_presented_ids': h['presented_ids'], 'full_presented_ids': f['presented_ids'],
                                    'hybrid_tokens': h['context_tokens'], 'full_tokens': f['context_tokens']})
    update(directory / 'paired-evidence.json', evidence_differences)
    # Keep official event-count macro averaging separate from an ordinary QA mean.
    grouped = defaultdict(lambda: defaultdict(list))
    for row in judgments:
        case = cases[row['case_id']]
        if row['metric'] == 'epbench' and case.category == 'all' and row.get('score') is not None:
            grouped[(row['arm'], str(case.truth['bin']))][case.id].append(row['score'])
    results['epbench_simple_recall'] = {}
    for arm in ('hybrid', 'full'):
        bins = {b: statistics.mean(statistics.mean(v) for v in items.values())
                for (a, b), items in grouped.items() if a == arm}
        results['epbench_simple_recall'][arm] = {'bins': bins, 'mean_across_present_bins': statistics.mean(bins.values()) if bins else None}
    results['epbench_chronological_awareness'] = {}
    for arm in ('hybrid', 'full'):
        latest = results['answers'].get('epbench/latest')
        chrono = results['answers'].get('epbench/chronological-required-complete-tau')
        results['epbench_chronological_awareness'][arm] = (latest[arm] + chrono[arm]) / 2 if latest and chrono else None
    update(directory / 'report.json', results)
    lines = [f'# Akasha 与 Hybrid：{phase} 结果', '',
             '状态：已完成；回答执行失败保留并计入分母。' if results['complete'] else '状态：尚未完成或评分待补。以下数字仅用于进度和故障诊断，不是最终质量结论。', '',
             f'冻结查询数：{len(cases)}；检索记录：{len(retrieval)}；回答记录：{len(answers)}；'
             f'评分记录：{len(judgments)}；时间排序评分：{len(chronology)}。', '',
             '主预算为 4,000 个 o200k_base token，最多 45 个 turn；同原文、向量、回答模型与评分规则。', '',
             '| 指标 | Hybrid | Akasha | 差值 | 95% 配对区间 | 题数/簇数 |',
             '|---|---:|---:|---:|---|---:|']
    for key, value in {**results['retrieval'], **results['answers']}.items():
        if value is None:
            continue
        interval = value['interval_95']
        shown = '—' if interval is None else f'[{100 * interval[0]:.2f}, {100 * interval[1]:.2f}]'
        lines.append(f"| {key} | {100 * value['hybrid']:.2f} | {100 * value['full']:.2f} | {100 * value['difference']:+.2f} | {shown} | {value['queries']}/{value['clusters']} |")
    lines += ['', '分数以 0–100 展示；差值为百分点或同尺度分数，时间顺序 tau 可为负。三次回答先按题平均，不采用 best-of-3。', '',
              'EpBench 仅有三个正式故事，按故事重采样的区间不证明广泛故事泛化；默认长篇与开发短篇共享世界。InMind 区间条件于同一固定背景，开发曝光任务另外标记。', '',
              '索引大小为同一 pickle 协议的内存快照字节数，含原文/向量引用结构，不是生产 SQLite 文件大小。检索延迟使用已缓存向量，排除模型服务、建库和只读状态检查。', '',
              '回答与评分采用 gpt-5.6-sol / max，是内部组件对照，不是官方榜单模型配置。输出 4096 为请求参数，当前 provider 不实施硬上限。未验证完整 Agent 生命周期或自然遗忘。计算在 Mac 执行，产物同步至 WSL；此处耗时不是 WSL 主机性能。', '',
              f"回答失败：{results['failed_answer_attempts']}；评分调用失败：{results['failed_scoring_attempts']}；补判调用：{results['scoring_recovery_attempts']}；评分 pending：{results['pending_judgments']}。回答失败的传播记录不作为新的 API 失败重复计数。完整用量、分组及逐题记录见同目录 JSON / JSONL。"]
    (directory / 'report.md').write_text('\n'.join(lines) + '\n')
    return results
