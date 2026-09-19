from datetime import UTC, datetime
from types import SimpleNamespace
from dataclasses import replace
import numpy as np
import pytest
from plugins.akasha.domain.model import Turn
from plugins.akasha.recalls import ProgramSource, query_memory
from plugins.reply.plugin import restrict_tools


def fixture():
    turns = [Turn(node_id=i, turn_id=f't{i}', session_key=f's{i}', user_seq=0,
        user_message_id=f'u{i}', assistant_message_id=f'a{i}',
        started_at=f'2020-01-0{i+1}T00:00:00+00:00', committed_at=f'2020-01-0{i+1}T00:00:00+00:00',
        user_text='fact', assistant_text='reply', user_dense=np.array(v,dtype=np.float32),
        assistant_dense=None, user_terms=(), assistant_terms=(), inter_gap_seconds=None)
        for i,v in enumerate(([1.,0.],[.6,.8],[0.,1.]))]
    def prohibited(*args,**kwargs):raise AssertionError('baseline must not execute graph retrieval')
    cycle=SimpleNamespace(turns=turns,state_version=3,inhibited_nodes={1},retrieve=prohibited)
    state=SimpleNamespace(legacy_prefix=SimpleNamespace(count=3))
    return cycle,state


@pytest.mark.parametrize('mode', ['none','dense'])
def test_baselines_bypass_graph_and_respect_forgetting(mode):
    cycle,state=fixture()
    record=query_memory(cycle,state,learning_binding='fixed',text='question',dense=np.array([1.,0.]),
        stamp=datetime(2020,1,4,tzinfo=UTC),source=ProgramSource(key='q',query='question'),limit=2,retrieval_mode=mode)
    assert record.retrieval_mode==mode and record.pushes==0 and record.active_basin_count==0
    assert [x.node_id for x in record.hits]==([] if mode=='none' else [0,2])
    assert cycle.state_version==3


def test_dense_uses_assistant_vectors_and_cosine_order_instead_of_recency():
    cycle,state=fixture()
    cycle.inhibited_nodes=set()
    cycle.turns[2]=replace(cycle.turns[2],assistant_dense=np.array([.8,.6]))
    record=query_memory(cycle,state,learning_binding='fixed',text='question',dense=np.array([1.,0.]),
        stamp=datetime(2020,1,4,tzinfo=UTC),source=ProgramSource(key='q',query='question'),limit=2,retrieval_mode='dense')
    assert [x.node_id for x in record.hits]==[0,2]
    assert all(x.lane=='dense' for x in record.hits)


def test_reply_restriction_excludes_files_shell_and_unknown_names():
    refs=tuple(SimpleNamespace(name=x) for x in ['shell','read_file','recall_memory','tool_search'])
    assert restrict_tools(refs,None)==refs
    assert restrict_tools(refs,())==()
    assert [x.name for x in restrict_tools(refs,('recall_memory','tool_search'))]==['recall_memory','tool_search']
    with pytest.raises(ValueError,match='unavailable'):restrict_tools(refs,('misspelled',))


def test_vector_reuse_excludes_prior_qa_and_verifies_history_content(tmp_path):
    from session.log import MessageLog,SessionAttributes
    from session.message import Input,ContentPart,ContentReferences
    from session.embedding_store import MessageEmbeddings
    from eval.memory_ablation import message_text,seed_history_embeddings
    source=MessageLog(tmp_path/'source.db');target=MessageLog(tmp_path/'target.db')
    def write(log,session,identity,text):
        log.ensure_session(session,SessionAttributes('internal','eligible'))
        return log.writer(session,author='user',source='conversation',body_types=(Input,),
            content={'text':lambda _:ContentReferences()}).append(identity,Input((ContentPart('text',text),)))
    try:
        history=write(source,'lme:example:history:0','original','original history')
        qa=write(source,'programmatic:eval:previous','answer','previous test answer')
        for message in [history,qa]:MessageEmbeddings(source).bind(message_text).save(message,model='space',embedding=[.6,.8])
        write(target,'lme:example:history:0','original','original history')
        rt=SimpleNamespace(core=SimpleNamespace(message_log=target))
        seeded=seed_history_embeddings(rt,tmp_path/'source.db',model='space',dimension=2)
        assert seeded['rows']==1
        assert target.reader('programmatic:eval:previous').snapshot()==()
        assert seed_history_embeddings(rt,tmp_path/'source.db',model='space',dimension=2)['sha256']==seeded['sha256']
        with pytest.raises(ValueError,match='space mismatch'):
            seed_history_embeddings(rt,tmp_path/'source.db',model='different-space',dimension=2)
    finally:source.close();target.close()
