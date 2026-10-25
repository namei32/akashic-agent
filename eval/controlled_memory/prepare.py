from __future__ import annotations

from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import random
import re
import sys

from .common import BASE, OLD_EP, Case, Suite, canonical, digest, file_digest, save


def exposure_audit(root, lme_ids):
    exposure = defaultdict(set)
    # This ledger records previous reservations as well as actual model exposure.
    previous = BASE / 'experiments/personal-memory-20260925-hawci7zw/data/exposure-ledger.json'
    ledger = json.loads(previous.read_text())
    for identity, names in ledger['question_exposures'].items():
        exposure['longmemeval:' + identity].update(names)
    inspected = []
    blocked = {'data', 'references', 'source', 'prepared', 'parquet-env', 'venv', '.venv',
               '__pycache__', 'node_modules', 'templates', 'release', 'splits-cache'}
    for base in (BASE / 'experiments', BASE / 'reports', BASE / 'smoke'):
        if not base.exists():
            continue
        for parent, directories, names in os.walk(base):
            parent = Path(parent)
            directories[:] = [n for n in directories if n not in blocked
                              and not any(s in n for s in ('workspace', 'plugin', 'model', 'cache', 'provider'))
                              and parent / n != root]
            for name in names:
                path = parent / name
                # Catalogs of all available data are not evidence of test exposure.
                if path.suffix not in {'.json', '.jsonl', '.md', '.csv'} or path.stat().st_size > 150_000_000:
                    continue
                if any(s in name for s in ('suite', 'exposure-ledger', 'human-review-key')):
                    continue
                text = path.read_text(errors='replace')
                identities = set(re.findall(r'longmemeval:([a-zA-Z0-9_]+)', text)) & lme_ids
                identities |= set(re.findall(r'"(?:question_id|case_id)"\s*:\s*"([^" :]+)"', text)) & lme_ids
                found = {'longmemeval:' + i for i in identities}
                found |= set(re.findall(r'inmind:\d+:(?:naive|query)', text))
                found |= set(re.findall(r'epbench:[0-9a-f]+:qa:\d+', text))
                if found:
                    inspected.append({'path': str(path), 'sha256': file_digest(path), 'case_count': len(found)})
                    for identity in found:
                        exposure[identity].add(str(path))
    # Frozen previous LME selections remain reserved even if a run was interrupted.
    for path in (BASE / 'experiments/hybrid-full-sol-max-20260923-g75o_dpp/lme-suite.json',
                 BASE / 'experiments/lme-recovery-20260925-c0isrbm3/suite.json'):
        for case in json.loads(path.read_text())['cases']:
            exposure[case['id']].add(str(path))
    exposure['longmemeval:e47becba'].add('previous early smoke documented in original audit')
    return exposure, inspected


def balanced(items, count, seed, key):
    rng = random.Random(seed)
    groups = defaultdict(list)
    for item in sorted(items, key=lambda x: x.id if hasattr(x, 'id') else x['question_id']):
        groups[key(item)].append(item)
    for bucket in groups.values():
        rng.shuffle(bucket)
    result = []
    while len(result) < count and any(groups.values()):
        for label in sorted(groups):
            if groups[label] and len(result) < count:
                result.append(groups[label].pop())
    return result


def ep_sample(cases, count, seed):
    # Include all three task families even when all-event cue templates dominate.
    targets = {'all': count * 3 // 5, 'latest': count // 5}
    targets['chronological'] = count - sum(targets.values())
    selected = []
    for category, n in targets.items():
        group = [c for c in cases if c.category == category]
        selected += balanced(group, n, seed, lambda c: str(c.truth['bin']))
    if len(selected) != count:
        raise ValueError('Insufficient published questions for predeclared EpBench strata')
    return selected


def prepare(root, h):
    from eval.episodic.datasets import epbench
    # Reuse the existing isolated Parquet dependency; no shared environment mutation.
    sys.path.append(str(OLD_EP / 'parquet-env/lib/python3.12/site-packages'))
    raw_path = BASE / 'data/longmemeval_s_cleaned.json'
    raw = json.loads(raw_path.read_text())
    exposure, inspected = exposure_audit(root, {r['question_id'] for r in raw})
    category = lambda r: 'abstention' if r['question_id'].endswith('_abs') else r['question_type']
    old = json.loads((BASE / 'experiments/hybrid-full-sol-max-20260923-g75o_dpp/lme-suite.json').read_text())
    dev_ids = {c['id'] for c in old['cases'] if c['split'] == 'dev'}
    dev_raw = balanced([r for r in raw if 'longmemeval:' + r['question_id'] in dev_ids], 12, 20261013, category)
    used_answer_sessions = {s for r in raw if 'longmemeval:' + r['question_id'] in exposure
                            for s in r.get('answer_session_ids', [])}
    available = [r for r in raw if 'longmemeval:' + r['question_id'] not in exposure
                 and not set(r.get('answer_session_ids', [])) & used_answer_sessions]
    main_raw = balanced(available, 120, 20261013, category)
    corpora, pilot, main = {}, [], []
    source_files = {str(raw_path): file_digest(raw_path)}
    for split, rows, target in [('dev', dev_raw, pilot), ('holdout', main_raw, main)]:
        path = root / 'data' / f'lme-{split}-raw.json'
        save(path, rows)
        source_files[str(path)] = file_digest(path)
        cs, qs = h.datasets.longmemeval(path, split, time_policy='provided-history')
        corpora.update((c.id, c) for c in cs)
        for q in qs:
            target.append(Case(id=q.id, dataset=q.dataset, corpus_id=q.corpus_id,
                               cluster_id=q.cluster_id, split=split, category=q.category,
                               question=q.question, cutoff=q.cutoff, question_at=q.question_at,
                               gold_ids=q.gold_ids, gold_level=q.gold_level, data_issues=q.data_issues,
                               truth={'answer': q.answer, 'unanswerable': q.unanswerable},
                               previously_exposed=q.id in exposure))
    from eval.episodic.schema import Suite as EpisodicSuite, verify_sources
    ep_old = EpisodicSuite.model_validate_json((OLD_EP / 'prepared/epbench-suite-v2.json').read_text())
    inmind_old = EpisodicSuite.model_validate_json((OLD_EP / 'prepared/inmind-suite-v2.json').read_text())
    verify_sources(ep_old)
    verify_sources(inmind_old)
    source_files.update(ep_old.source_files)
    source_files.update(inmind_old.source_files)
    for c in ep_old.corpora + inmind_old.corpora:
        corpora[c.id] = h.schema.Corpus.model_validate(c.model_dump(mode='json'))
    pilot += [Case.model_validate({**c.model_dump(mode='json'), 'previously_exposed': c.id in exposure})
              for c in ep_sample([c for c in ep_old.cases if c.split == 'dev'], 12, 20261013)]
    books = OLD_EP / 'data/epbench-books'
    long_books = [
        books / 'Udefault_Sdefault_seed0/model_claude-3-5-sonnet-20240620_itermax_10_Idefault_nbchapters_196_nbtokens_102870',
        books / 'Unews_Snews_seed1/model_claude-3-5-sonnet-20241022_itermax_10_Idefault_nbchapters_200_nbtokens_69090',
        books / 'Uscifi_Sscifi_seed2/model_claude-3-5-sonnet-20241022_itermax_10_Idefault_nbchapters_200_nbtokens_89736',
    ]
    for book in long_books:
        cs, qs = epbench(book, source_files, split='holdout')
        corpora.update((c.id, h.schema.Corpus.model_validate(c.model_dump(mode='json'))) for c in cs)
        main += [Case.model_validate({**c.model_dump(mode='json'), 'previously_exposed': c.id in exposure})
                 for c in ep_sample(qs, 40, 20261013)]
    naive = [c for c in inmind_old.cases if c.split == 'dev' and c.category == 'naive']
    chosen = balanced(naive, 6, 20261013, lambda c: c.truth['domain'])
    pilot_tasks = {c.corpus_id for c in chosen}
    exposed_tasks = {i.rsplit(':', 1)[0] for i in exposure if i.startswith('inmind:')} | pilot_tasks
    for c in inmind_old.cases:
        data = c.model_dump(mode='json')
        data['previously_exposed'] = c.corpus_id in exposed_tasks
        if c.corpus_id in pilot_tasks:
            pilot.append(Case.model_validate({**data, 'split': 'dev'}))
        main.append(Case.model_validate({**data, 'split': 'holdout'}))
    scorer = BASE / 'experiments/lme-recovery-20260925-c0isrbm3/references/longmemeval_evaluate_qa.py'
    source_files[str(scorer)] = file_digest(scorer)
    protocol = {
        'version': 2, 'seed': 20261013, 'tokenizer': 'o200k_base',
        'main_budget_tokens': 4000, 'retrieval_budgets_tokens': [2000, 4000, 8000],
        'native_reference_chars': 12000, 'global_turn_limit': 45,
        'full_limit': 40, 'candidate_limit_per_hybrid_lane': 100, 'rrf_k': 60,
        'repeats_pilot': 1, 'repeats_main': 3, 'tool_budget': 0,
        'model': 'gpt-5.6-sol', 'effort': 'max', 'embedding': 'text-embedding-v4', 'dimensions': 1024,
        'epbench_source': str(OLD_EP / 'data/epbench'), 'longmemeval_scorer': str(scorer),
        'learning': 'Frozen current MemoryCycle commit/retrieve component; queries are read-only; no Agent lifecycle claim',
        'clock': 'LongMemEval source dates/provided-history; EpBench/InMind acceptance order, source dates remain in text',
        'epbench_overlap': 'Default long shares a world/events with the development short book; news/scifi are separate released worlds',
        'inmind_scope': 'All 125 tasks are regression; report previously unexposed tasks separately; shared background is not 125 independent backgrounds',
        'judging': 'Official rubrics; shared internal reader/judge configuration, not official leaderboard model configuration',
        'output_cap': '4096 requested; frozen provider does not enforce this as a hard cap',
    }
    for name, cases in [('pilot', pilot), ('main', main)]:
        keys = {c.corpus_id for c in cases}
        suite = Suite(source_files=source_files, protocol=protocol,
                      corpora=tuple(corpora[k] for k in sorted(keys)),
                      cases=tuple(sorted(cases, key=lambda c: (c.corpus_id, c.id))))
        save(root / 'data' / f'{name}-suite.json', suite)
    role_shapes = Counter(','.join(m.role for m in u.messages) for c in corpora.values() for u in c.units)
    audit = {
        'lme_total': len(raw), 'lme_exposed_or_reserved': sum('longmemeval:' + r['question_id'] in exposure for r in raw),
        'lme_available_without_exposed_answer_sessions': len(available), 'lme_formal': len(main_raw),
        'lme_formal_categories': dict(Counter(category(r) for r in main_raw)),
        'pilot_queries': len(pilot), 'main_queries': len(main),
        'pilot_by_dataset': dict(Counter(c.dataset for c in pilot)),
        'main_by_dataset': dict(Counter(c.dataset for c in main)),
        'inmind_previous_or_pilot_tasks': sorted(exposed_tasks),
        'history_role_shapes': dict(role_shapes),
        'exposure_sources': inspected, 'case_exposures': {k: sorted(v) for k, v in sorted(exposure.items())},
        'scope': 'Exposure means prior execution or explicit reservation, not merely membership in a raw public release. No quality outcomes used in sampling.',
    }
    save(root / 'data/exposure-audit.json', audit)
    save(root / 'protocol.json', protocol)
    print(canonical({k: v for k, v in audit.items() if k not in ('exposure_sources', 'case_exposures', 'inmind_previous_or_pilot_tasks')}), flush=True)
