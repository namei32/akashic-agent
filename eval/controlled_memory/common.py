from __future__ import annotations

from datetime import datetime
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

HARNESS = Path('/Users/namei/.codex/worktrees/public-memory-bench-20261003/akashic')
HARNESS_REF = '4a172d3e339ff806b5b2641426fe50e78c0130c5'
OLD_EP = Path('/Users/namei/.local/share/akashic-eval/experiments/episodic-20261007-sahyzo1i')
BASE = OLD_EP.parent.parent
WORKTREE = Path(__file__).resolve().parents[2]


def canonical(value):
    if isinstance(value, BaseModel):
        value = value.model_dump(mode='json')
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as stream:
        stream.write(canonical(value) + '\n')


def update(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(canonical(value) + '\n')
    temp.replace(path)


def jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open() as stream:
        return [json.loads(line) for line in stream if line.strip()]


def append(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        stream.write(canonical(value) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def bootstrap(root):
    import eval
    sys.dont_write_bytecode = True
    for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
        os.environ[name] = '1'
    os.environ['TIKTOKEN_CACHE_DIR'] = str(root / 'data/tokenizer-cache')
    source = root / 'source'
    manifest = json.loads((root / 'source-manifest.json').read_text())
    for relative, expected in manifest['files'].items():
        if file_digest(source / relative) != expected:
            raise ValueError(f'Frozen production source changed: {relative}')
    sys.path.insert(0, str(source))
    eval.__path__.append(str(HARNESS / 'eval'))
    paths = subprocess.check_output(['git', '-C', str(HARNESS), 'ls-tree', '-r', '--name-only', HARNESS_REF, 'eval/association'], text=True).splitlines()
    for relative in paths:
        if relative.endswith('.py'):
            expected = subprocess.check_output(['git', '-C', str(HARNESS), 'show', f'{HARNESS_REF}:{relative}'])
            if (HARNESS / relative).read_bytes() != expected:
                raise ValueError(f'Frozen supporting harness changed: {relative}')
    h = SimpleNamespace(**{name: importlib.import_module('eval.association.' + name)
                          for name in ('schema', 'embeddings', 'retrieval', 'client', 'datasets')})
    native = importlib.import_module('plugins.akasha.application.cycle')
    if Path(native.__file__).resolve() != source / 'plugins/akasha/application/cycle.py':
        raise ValueError('Wrong production memory engine imported')
    return h


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class Case(Record):
    id: str
    dataset: Literal['longmemeval', 'epbench', 'inmind']
    corpus_id: str
    cluster_id: str
    split: Literal['dev', 'holdout']
    category: str
    question: str
    cutoff: datetime
    question_at: datetime | None = None
    gold_ids: tuple[str, ...] = ()
    gold_level: Literal['message', 'session', 'none'] = 'message'
    data_issues: tuple[str, ...] = ()
    truth: dict[str, Any] = {}
    previously_exposed: bool = False


class Suite(Record):
    version: int = 2
    source_files: dict[str, str]
    protocol: dict[str, Any]
    corpora: tuple[Any, ...]
    cases: tuple[Case, ...]


def load_suite(path, h):
    data = json.loads(Path(path).read_text())
    for name, expected in data['source_files'].items():
        if file_digest(name) != expected:
            raise ValueError(f'Dataset/scoring source changed: {name}')
    data['corpora'] = tuple(h.schema.Corpus.model_validate(c) for c in data['corpora'])
    suite = Suite.model_validate(data)
    if len({c.id for c in suite.cases}) != len(suite.cases):
        raise ValueError('Duplicate query identity')
    histories = {c.id: c for c in suite.corpora}
    for case in suite.cases:
        known = {m.id if case.gold_level == 'message' else m.source_session_id or m.session_id
                 for u in histories[case.corpus_id].visible(case.cutoff).units for m in u.messages}
        if not set(case.gold_ids) <= known:
            raise ValueError(f'Gold not in visible history: {case.id}')
    return suite


def code_identity(root):
    import importlib.metadata
    files = {}
    for base in (WORKTREE / 'eval/controlled_memory', WORKTREE / 'eval/episodic', HARNESS / 'eval/association'):
        for path in sorted(base.rglob('*.py')):
            files[str(path)] = file_digest(path)
    for name in ('provider_server.py', 'refresh_desktop_auth.py', 'provider-source-path.txt', 'python-path.txt'):
        path = root / 'private-provider' / name
        files[str(path)] = file_digest(path)
    for path in sorted((root / 'private-provider/model-plugin-home').rglob('*.py')):
        files[str(path)] = file_digest(path)
    return {'files': files, 'source_manifest_sha256': file_digest(root / 'source-manifest.json'),
            'python': sys.version, 'packages': {name: importlib.metadata.version(name)
                                              for name in ('numpy', 'pydantic', 'jieba', 'tiktoken')}}


def checkpoint(root, stage, **values):
    payload = {'stage': stage, **values}
    update(root / 'status.json', payload)
    print(canonical(payload), flush=True)
