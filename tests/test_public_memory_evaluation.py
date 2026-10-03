"""Experiment boundary checks: label isolation, checkpointing and official score semantics."""
from contextlib import closing
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

from eval.association.schema import Case, Corpus, Evidence, FeedbackAction, RubricItem, Settings, Suite, Unit
from eval.association.embeddings import VectorCache
from eval.association.retrieval import Full, Hybrid, materialize
from eval.association.runner import InfrastructureStop, jsonl, run_answers, run_retrieval
from eval.association.public_scoring import MemoraScorer, parse_memora_verdict, run_public_judgments
from eval.association.scoring import OfficialScorers
from eval.association.memora import prepare_memora


def fixture(tmp_path):
    stamp = datetime(2020, 1, 1, tzinfo=UTC)
    unit = Unit(id="u0", messages=(Evidence(id="m0", session_id="s0", speaker="user", role="user", at=stamp, text="The library is in York"),))
    corpus = Corpus(id="history", adapter="fixture", units=(unit,))
    cases = (Case(id="q0", dataset="locomo", corpus_id="history", cluster_id="c0", split="dev", category="single-hop",
                  official_category=4, question="Where is the library?", cutoff=stamp+timedelta(days=1), answer="York"),)
    suite = Suite(sources={"fixture": "true"}, corpora=(corpus,), cases=cases)
    settings = Settings(model="fixture", embedding_model="fixture", dimensions=2, budget_chars=500, repeats=5)
    cache = VectorCache(tmp_path / "vectors.sqlite3", model="fixture", dimensions=2, space="fixture", writable=True)
    cache.put(unit.input_text, [1, 0]);cache.put(cases[0].question, [1, 0])
    return suite, settings, cache


@pytest.mark.asyncio
async def test_network_guard_saves_three_failures_and_resume_does_not_regenerate_them(tmp_path):
    suite, settings, cache = fixture(tmp_path)
    with closing(cache):run_retrieval(suite, cache, tmp_path/'retrieval', settings, split='dev')
    class TransportError(Exception):pass
    class Broken:
        calls = 0
        async def complete(self, *args):
            self.calls += 1
            raise TransportError("synthetic outage")
    broken = Broken()
    with pytest.raises(InfrastructureStop):
        await run_answers(suite, tmp_path/'retrieval', tmp_path/'answers', settings, broken)
    prior = jsonl(tmp_path/'answers/answers.jsonl')
    assert broken.calls == len(prior) == 3
    class Healthy:
        calls = 0
        async def complete(self, *args):
            self.calls += 1
            return {"text":"York", "model":"fixture", "effort":"max"}
    healthy = Healthy()
    await run_answers(suite, tmp_path/'retrieval', tmp_path/'answers', settings, healthy)
    rows = jsonl(tmp_path/'answers/answers.jsonl')
    assert rows[:3] == prior and len(rows) == 10 and healthy.calls == 7
    assert json.loads((tmp_path/'answers/completion.json').read_text())['failures'] == 3
    await run_answers(suite, tmp_path/'retrieval', tmp_path/'answers', settings, healthy)
    assert healthy.calls == 7


def test_feedback_maps_original_messages_and_both_arms_suppress_target(tmp_path):
    suite, settings, cache = fixture(tmp_path)
    stamp = suite.corpora[0].units[0].messages[0].at + timedelta(days=1)
    unit = Unit(id='u1', messages=(Evidence(id='m1', session_id='s1', speaker='user',role='user',at=stamp,text='Forget the library location'),),
                feedback=(FeedbackAction(action='forget',target_message_ids=('m0',),source_call_id='visible-normalizer-call'),))
    corpus = suite.corpora[0].model_copy(update={'units':(*suite.corpora[0].units,unit)})
    cache.put(unit.input_text,[0,1])
    with closing(cache):
        turns,state = materialize(corpus,cache)
        assert turns[1].feedback.forget_nodes == (0,)
        q=suite.cases[0].model_copy(update={'cutoff':stamp+timedelta(days=1)})
        for index in [Hybrid(turns,settings),Full(turns,state,settings)]:
            candidates,_=index.query(q,cache.get(q.question))
            assert all(item['node_id'] != 0 for item in candidates)


def test_official_memora_formula_handles_no_forgetting_without_claiming_success(tmp_path):
    path=tmp_path/'scorer.py'
    path.write_text('''def fama_score(p,np,f,nf):
    mpa=p/np if np else 0.0
    faa=f/nf if nf else 1.0
    lam=nf/(np+nf) if np+nf else 0.0
    return max(0.0,mpa-lam*(1.0-faa))
class Q:
    def _evaluate_with_single_judge(self,generated_answer,eval_question):
        system_prompt="judge"
        user_prompt=f"{generated_answer}\\n{eval_question}"
''')
    from eval.association.schema import file_digest
    scorer=MemoraScorer(path,file_digest(path))
    assert scorer.fama(1,2,0,1) == pytest.approx(1/6)
    assert scorer.fama(1,2,0,0) == .5
    assert scorer.fama(0,0,0,0) == 0
    criterion=RubricItem(id='criterion',question='Does it mention York?',expected_answer='yes',kind='memory_presence')
    system,user=scorer.prompt('York',criterion)
    assert user == 'York\nDoes it mention York?' and 'expected_answer' not in user
    assert parse_memora_verdict('{"answer":"no"}')[0] == 'no'
    with pytest.raises(ValueError):parse_memora_verdict('{"answer":"unclear"}')


def test_memora_keeps_full_dialogue_but_never_indexes_hidden_metadata(tmp_path):
    personas=['financial_analyst','academic_researcher',*[f'persona{i}' for i in range(8)]]
    for name in personas:
        root=tmp_path/name;(root/'conversations').mkdir(parents=True)
        (root/'conversations/session_0001.json').write_text(json.dumps({'session_id':1,'date':'2025-06-07','operation_details':{'secret':'HIDDEN-LABEL'},'persona':'SECRET-DOSSIER',
            'conversation':[{'speaker':'user_agent','message':'Visible text even when share_memory is false','share_memory':False}, {'speaker':'ai_agent','message':'Noted'}]}))
        questions={category:[{'question_id':category+str(i),'question':'Question?','question_date':'2025-06-07',
            'memory_evidence':{'entry':{'session_id':1}},'forgetting_evidence':{'secret':'GOLD-ONLY'},
            'evaluation':{'evaluation_questions':[{'evaluation_question_id':'e'+str(i),'evaluation_question':'Does it mention the fact?',
            'expected_answer':'yes','evaluation_type':'memory_presence'}]}} for i in range(5)] for category in ['remembering','reasoning','recommending']}
        (root/('evaluation_questions_'+name+'.json')).write_text(json.dumps({'questions':questions}))
    suite,pilot,audit=prepare_memora(tmp_path)
    assert len(suite.cases)==150 and len(pilot.cases)==12 and len({q.cluster_id for q in pilot.cases})==2
    corpus_json=json.dumps([c.model_dump(mode='json') for c in suite.corpora])
    assert 'HIDDEN-LABEL' not in corpus_json and 'SECRET-DOSSIER' not in corpus_json and 'GOLD-ONLY' not in corpus_json
    assert 'Visible text even when share_memory is false' in corpus_json and 'financial_analyst' not in corpus_json
    assert not audit['missing_evidence_cases']


@pytest.mark.asyncio
async def test_public_memora_scoring_checks_each_rubric_and_reuses_complete_scores(tmp_path):
    suite, settings, cache = fixture(tmp_path)
    settings = settings.model_copy(update={'repeats':1})
    criteria = (RubricItem(id='present',question='Does it mention York?',expected_answer='yes',kind='memory_presence'),
                RubricItem(id='absent',question='Does it mention Leeds?',expected_answer='no',kind='forgetting_absence'))
    case = suite.cases[0].model_copy(update={'dataset':'memora','rubrics':criteria,'category':'remembering'})
    suite=suite.model_copy(update={'cases':(case,)})
    with closing(cache):run_retrieval(suite,cache,tmp_path/'retrieval',settings,split='dev')
    class Reader:
        async def complete(self,*args):return {'text':'York','model':'fixture','effort':'max'}
    await run_answers(suite,tmp_path/'retrieval',tmp_path/'answers',settings,Reader())
    script=tmp_path/'scorer.py'
    script.write_text('''def fama_score(p,np,f,nf):
    return max(0.0,(p/np if np else 0.0)-(nf/(np+nf) if np+nf else 0.0)*(1-(f/nf if nf else 1.0)))
class Q:
    def _evaluate_with_single_judge(self,generated_answer,eval_question):
        system_prompt="judge"
        user_prompt=f"RESPONSE:{generated_answer}\\nQUESTION:{eval_question}"
''')
    class Judge:
        calls=0
        async def complete(self,system,user,tokens):
            self.calls+=1
            assert 'hybrid' not in user and 'full' not in user and 'expected_answer' not in user
            verdict='yes' if 'York?' in user else 'no'
            return {'text':json.dumps({'answer':verdict}), 'model':'fixture','effort':'max','call_record_id':str(self.calls)}
    from eval.association.schema import file_digest
    judge=Judge()
    await run_public_judgments(suite,tmp_path/'answers',tmp_path/'judgments',settings,judge,memora=script,memora_sha=file_digest(script))
    rows=jsonl(tmp_path/'judgments/judgments.jsonl')
    assert len(rows)==2 and judge.calls==4 and all(r['fama']==1 and r['forgetting_absence_accuracy']==1 for r in rows)
    await run_public_judgments(suite,tmp_path/'answers',tmp_path/'judgments',settings,judge,memora=script,memora_sha=file_digest(script))
    assert judge.calls==4
