"""Behavioral checks for isolated replay, read-only QA and honest scoring."""
import asyncio
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from eval.longmemeval.dataset import LMEInstance, LMETurn
from eval.longmemeval.ingest import ingest_instance
from eval.longmemeval.metrics import score_results
from eval.longmemeval.runtime import BenchmarkRuntime, archive_partial_workspace, ask, parse_date
from session.log import MessageLog
from session.message import Input, Output


def sample():
    return LMEInstance('q1', 'multi-session', 'What changed?', 'blue', '2025/01/04 (Sat) 10:00',
        ['s1', 's2'], ['2025/01/01 (Wed) 09:00', '2025/01/03 (Fri) 12:00'], [
            [LMETurn('user', 'My choice is red.'), LMETurn('assistant', 'Noted.')],
            [LMETurn('user', 'My choice is blue.'), LMETurn('assistant', 'Updated.')],
        ])


@pytest.mark.asyncio
async def test_replay_preserves_separate_sessions_dates_and_original_messages(tmp_path):
    log = MessageLog(tmp_path / 'sessions.db')
    rt = SimpleNamespace(core=SimpleNamespace(message_log=log), workspace=tmp_path, started=False)
    try:
        case = sample()
        assert await ingest_instance(rt, case) == 4
        first = log.reader('lme:q1:history:0').snapshot()
        second = log.reader('lme:q1:history:1').snapshot()
        assert isinstance(first[0].body, Input) and isinstance(first[1].body, Output)
        assert first[0].body.parts[0].value == 'My choice is red.'
        assert second[0].recorded_at == parse_date(case.haystack_dates[1])
        assert log.catalog().attributes(first[0].session_id).learning == 'eligible'
        assert await ingest_instance(rt, case, force=True) == 4
        assert log.reader(first[0].session_id).snapshot() == first
        case.haystack_sessions[0][0].content = 'tampered'
        with pytest.raises(ValueError, match='Historical input changed'):
            await ingest_instance(rt, case)
        assert log.reader(first[0].session_id).snapshot() == first
    finally:
        log.close()


def test_failures_and_unjudged_answers_remain_in_score_denominator():
    rows = [dict(question_id='a', question_type='single-session-user', predicted_answer='yes',
                 gold_answer='yes', error=None, judge_correct=True),
            dict(question_id='b', question_type='single-session-user', predicted_answer='',
                 gold_answer='yes', error='timeout', judge_correct=None),
            dict(question_id='c_abs', question_type='single-session-user', predicted_answer='unknown',
                 gold_answer='irrelevant', error=None, judge_correct=None)]
    scores = score_results(rows)
    assert scores['overall']['judge_acc'] == 0.3333
    assert scores['overall']['errors'] == 1
    assert scores['overall']['judge_missing'] == 1
    assert scores['overall']['judge_conditional_acc'] == 1.0
    assert scores['by_type']['abstention']['n'] == 1


def test_partial_recovery_preserves_evidence_and_refuses_unowned_directories(tmp_path):
    live = tmp_path / 'live'
    live.mkdir()
    (live / 'sessions.db').write_bytes(b'protected')
    with pytest.raises(ValueError, match='unmarked'):
        archive_partial_workspace(live)
    assert (live / 'sessions.db').read_bytes() == b'protected'
    (live / '.benchmark-case.json').write_text('{}')
    archive_partial_workspace(live)
    archived = list(tmp_path.glob('live.partial-*'))
    assert len(archived) == 1
    assert (archived[0] / 'sessions.db').read_bytes() == b'protected'
    assert not list(live.iterdir())


@pytest.mark.asyncio
async def test_qa_uses_programmatic_reply_and_never_learns_or_deletes_history(tmp_path):
    from tests.test_default_reply import application
    from agent.plugin_contracts import timekit
    original_clock = timekit.utcnow
    def add_source(root):
        shutil.copytree(Path(__file__).parents[1] / 'plugins/programmatic', root / 'programmatic')
        provider=root / 'test_provider/plugin.py'
        text=provider.read_text()
        text='from agent.plugin_contracts import timekit\n'+text
        text=text.replace('handle.write("once\\n")', 'handle.write(timekit.utcnow().isoformat()+"\\n")')
        provider.write_text(text)
    async with application(tmp_path, replying=True, extra_sources=add_source) as (log, host):
        rt = BenchmarkRuntime(SimpleNamespace(message_log=log, plugin_manager=host), tmp_path, None, started=True)
        answer, trace, session = await ask(rt, question='Record the evidence and finish.',
            question_date='2026-01-02', session_prefix='qa-test', timeout_s=5)
        assert answer == 'finished'
        assert trace[0]['calls'][0]['name'] == 'write_evidence'
        assert (tmp_path / 'effect.txt').read_text().strip()==parse_date('2026-01-02').isoformat()
        assert log.catalog().attributes(session).learning == 'excluded'
        before = log.reader(session).snapshot()
        assert before[0].recorded_at == parse_date('2026-01-02')
        answer2, _, other = await ask(rt, question='Another request.', question_date='2026-01-03',
                                     session_prefix='qa-test', timeout_s=5)
        assert answer2 == 'finished' and other != session
        assert log.reader(session).snapshot() == before
        assert timekit.utcnow is original_clock
