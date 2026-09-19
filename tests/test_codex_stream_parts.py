"""Responses text finality is scoped to an item and content/summary index."""
import json

import httpx
import pytest

from agent.plugin_composition import ModelRequest
from agent.plugin_composition.models import TransportError
from plugins.codex.responses import _consume_stream


async def consume(events):
    emitted=[]
    async def delta(value):emitted.append(value)
    terminal={"type":"response.completed", "response":{}}
    wire='\n\n'.join('data: '+json.dumps(e) for e in [*events,terminal])+'\n\n'
    response=httpx.Response(200,text=wire,headers={"content-type":"text/event-stream"})
    result=await _consume_stream(response,ModelRequest(messages=(),max_output_tokens=100,on_delta=delta),'test',())
    return result,emitted


@pytest.mark.asyncio
@pytest.mark.parametrize('family,index_name,attribute',[
    ('output_text','content_index','content'),
    ('reasoning_summary_text','summary_index','thinking'),
    ('reasoning_text','content_index','thinking'),
])
async def test_multiple_parts_and_done_suffixes_do_not_compare_against_other_parts(family,index_name,attribute):
    events=[]
    for index,text in enumerate(('first ', 'second')):
        base={'item_id':'same-item','output_index':0,index_name:index}
        events.extend([{**base,'type':f'response.{family}.delta','delta':text[:-1]},
                       {**base,'type':f'response.{family}.done','text':text},
                       {**base,'type':f'response.{family}.done','text':text}])
    result,emitted=await consume(events)
    assert getattr(result,attribute)=='first second'
    key='content_delta' if attribute=='content' else 'thinking_delta'
    assert ''.join(e.get(key,'') for e in emitted)=='first second'


@pytest.mark.asyncio
async def test_different_items_with_the_same_content_index_are_independent():
    result,_=await consume([
        {'type':'response.output_text.delta','item_id':'a','content_index':0,'delta':'A'},
        {'type':'response.output_text.done','item_id':'a','content_index':0,'text':'A'},
        {'type':'response.output_text.done','item_id':'b','content_index':0,'text':'B'},
    ])
    assert result.content=='AB'


@pytest.mark.asyncio
async def test_conflicting_text_within_one_part_still_fails_without_retry():
    with pytest.raises(TransportError,match='done text') as captured:
        await consume([
            {'type':'response.output_text.delta','item_id':'a','content_index':0,'delta':'A'},
            {'type':'response.output_text.done','item_id':'a','content_index':0,'text':'B'},
        ])
    assert captured.value.retryable is False
