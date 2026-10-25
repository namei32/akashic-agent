from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sys
import traceback

from .common import bootstrap, checkpoint, code_identity, file_digest, jsonl, load_suite, save, update, WORKTREE, HARNESS


def archive_inputs(root, suites):
    index = {}
    for suite in suites:
        for original, sha in suite.source_files.items():
            path = Path(original)
            if root in path.parents:
                index[original] = str(path.relative_to(root))
                continue
            relative = Path('published-sources') / sha / path.name
            dest = root / relative
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                # Same-host archival link does not duplicate a large public dataset.
                # The WSL publisher sends the full contents, not the host hard-link metadata.
                os.link(path, dest)
            if file_digest(dest) != sha:
                raise ValueError('Archived public source changed')
            index[original] = str(relative)
    path = root / 'published-source-index.json'
    if not path.exists():
        save(path, index)
    for prefix, base in [('controlled_memory', WORKTREE / 'eval/controlled_memory'),
                         ('episodic', WORKTREE / 'eval/episodic'),
                         ('association', HARNESS / 'eval/association')]:
        for source in base.glob('*.py'):
            dest = root / 'evaluator-code' / prefix / source.name
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest)


async def run(root, h, args):
    from .calls import answer_schedule, judge_schedule, chrono_schedule, execute
    from .retrieval import embed_missing, retrieve
    from .report import report
    from .storage import WSLSync
    suites = {phase: load_suite(root / 'data' / f'{phase}-suite.json', h) for phase in ('pilot', 'main')}
    archive_inputs(root, suites.values())
    identity = code_identity(root)
    frozen = root / 'evaluator-manifest.json'
    if frozen.exists():
        if json.loads(frozen.read_text()) != identity:
            raise ValueError('Evaluation source changed after freezing; do not resume this run')
    else:
        save(frozen, identity)
    sync = WSLSync(root)
    sync.sync(force=True)
    os.environ['AKASHIC_EVAL_RUN_ROOT'] = str(root / 'private-provider')
    settings = h.schema.Settings(repeats=1, seed=20261013)
    phase = 'pilot'
    try:
        async with h.client.runtime_client(root / 'model.toml', root / 'model-workspaces' / 'controller', settings) as client:
            for phase in ('pilot', 'main'):
                if args.pilot_only and phase == 'main':
                    break
                suite = suites[phase]
                await embed_missing(root, suite, h, client, phase=phase)
                sync.sync(force=True, vectors=True)
                rows = retrieve(root, suite, h, settings, phase=phase)
                report(root, suite, phase=phase)
                sync.sync(force=True)
                if args.retrieval_only:
                    continue
                repeats = 1 if phase == 'pilot' else 3
                schedule = answer_schedule(suite, rows, repeats)
                answers = await execute(root, suite, schedule, client, settings, sync, phase=phase, stage='answers')
                judgments = await execute(root, suite, judge_schedule(suite, answers), client, settings, sync,
                                          phase=phase, stage='judgments', judging=True)
                await execute(root, suite, chrono_schedule(suite, judgments), client, settings, sync,
                              phase=phase, stage='chronology', judging=True)
                result = report(root, suite, phase=phase)
                sync.sync(force=True)
                if not result['complete'] or result['pending_judgments']:
                    raise RuntimeError('Unsettled scoring blocks advancement; preserve saved answers')
                # Known answer failures remain zero-scored in the full denominator.
                # Their immutable history must not permanently block a recovered run.
                if code_identity(root) != identity:
                    raise RuntimeError('Evaluation implementation changed while running')
                checkpoint(root, phase + '_complete', answer_calls=len(answers), judge_calls=len(judgments))
    except BaseException as error:
        checkpoint(root, 'stopped', phase=phase, error=f'{type(error).__name__}: {error}',
                   traceback=traceback.format_exc(), pending_work='Resume the same frozen schedule after resolving the recorded error; never regenerate completed answers.')
        try:
            report(root, suites[phase], phase=phase)
            sync.sync(force=True)
        except Exception as storage_error:
            print('Final checkpoint publication failed: ' + str(storage_error), flush=True)
        raise
    checkpoint(root, 'pilot_complete' if args.pilot_only else 'retrieval_complete' if args.retrieval_only else 'complete')
    sync.sync(force=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('prepare', 'run', 'sync', 'report'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--pilot-only', action='store_true')
    parser.add_argument('--retrieval-only', action='store_true')
    args = parser.parse_args()
    h = bootstrap(args.root)
    if args.command == 'prepare':
        from .prepare import prepare
        prepare(args.root, h)
    elif args.command == 'sync':
        from .storage import WSLSync
        WSLSync(args.root).sync(force=True)
    elif args.command == 'report':
        from .report import report
        for phase in ('pilot', 'main'):
            report(args.root, load_suite(args.root / 'data' / f'{phase}-suite.json', h), phase=phase)
    else:
        asyncio.run(run(args.root, h, args))


if __name__ == '__main__':
    main()
