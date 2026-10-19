"""Responses text completion belongs to one item/part, not the whole response."""

import json

import httpx
import pytest

from agent.plugin_composition import ModelRequest, TransportError
from plugins.codex.responses import _consume_stream


async def consume(events, deltas):
    async def on_delta(delta):
        deltas.append(delta)

    wire_events = [*events, {"type": "response.completed", "response": {}}]
    body = "".join(f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in wire_events)
    response = httpx.Response(200, text=body)
    try:
        return await _consume_stream(
            response,
            ModelRequest(messages=[], max_output_tokens=128, on_delta=on_delta),
            "test-binding",
            (),
        )
    finally:
        await response.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,part_index,result_field,delta_field",
    [
        ("output_text", "content_index", "content", "content_delta"),
        ("reasoning_summary_text", "summary_index", "thinking", "thinking_delta"),
        ("reasoning_text", "content_index", "thinking", "thinking_delta"),
    ],
)
@pytest.mark.parametrize(
    "first_scope,second_scope,second_part",
    [
        ({"item_id": "item-a", "output_index": 0}, {"item_id": "item-b", "output_index": 1}, 0),
        ({"item_id": "item-a", "output_index": 0}, {"item_id": "item-a", "output_index": 0}, 1),
        ({"output_index": 0}, {"output_index": 1}, 0),
        ({"item_id": "item-a"}, {"item_id": "item-b"}, 0),
    ],
    ids=["separate-items", "separate-parts", "index-only", "id-only"],
)
async def test_done_reconciles_only_its_own_text_part(
    kind, part_index, result_field, delta_field, first_scope, second_scope, second_part,
):
    first = {**first_scope, part_index: 0}
    second = {**second_scope, part_index: second_part}
    events = [
        {"type": f"response.{kind}.delta", **first, "delta": "第一"},
        {"type": f"response.{kind}.done", **first, "text": "第一段。"},
        {"type": f"response.{kind}.delta", **second, "delta": "第二段"},
        {"type": f"response.{kind}.done", **second, "text": "第二段。"},
        {"type": f"response.{kind}.done", **second, "text": "第二段。"},
    ]
    deltas = []

    result = await consume(events, deltas)

    assert getattr(result, result_field) == "第一段。第二段。"
    assert deltas == [
        {delta_field: "第一"}, {delta_field: "段。"},
        {delta_field: "第二段"}, {delta_field: "。"},
    ]


@pytest.mark.asyncio
async def test_done_only_part_and_interleaved_output_keep_received_text():
    first = {"item_id": "message-a", "output_index": 0, "content_index": 0}
    second = {"item_id": "message-b", "output_index": 1, "content_index": 0}
    events = [
        {"type": "response.output_text.delta", **first, "delta": "A"},
        {"type": "response.output_text.done", **second, "text": "B"},
        {"type": "response.output_text.done", **first, "text": "A!"},
    ]
    deltas = []

    result = await consume(events, deltas)

    assert result.content == "AB!"
    assert deltas == [{"content_delta": "A"}, {"content_delta": "B"}, {"content_delta": "!"}]


@pytest.mark.asyncio
async def test_raw_reasoning_and_summary_use_separate_part_buffers():
    scope = {"item_id": "reasoning-a", "output_index": 0}
    events = [
        {"type": "response.reasoning_text.delta", **scope, "content_index": 0, "delta": "过程"},
        {"type": "response.reasoning_text.done", **scope, "content_index": 0, "text": "过程。"},
        {"type": "response.reasoning_summary_text.delta", **scope, "summary_index": 0, "delta": "摘要"},
        {"type": "response.reasoning_summary_text.done", **scope, "summary_index": 0, "text": "摘要。"},
    ]
    deltas = []

    result = await consume(events, deltas)

    assert result.thinking == "过程。摘要。"
    assert deltas == [
        {"thinking_delta": "过程"}, {"thinking_delta": "。"},
        {"thinking_delta": "摘要"}, {"thinking_delta": "。"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,index_field",
    [("output_text", "content_index"), ("reasoning_summary_text", "summary_index"),
     ("reasoning_text", "content_index")],
)
async def test_conflicting_done_for_same_part_still_fails_without_retry(kind, index_field):
    scope = {"item_id": "item-a", "output_index": 0, index_field: 0}
    deltas = []

    with pytest.raises(TransportError, match="done text.*delta") as caught:
        await consume([
            {"type": f"response.{kind}.delta", **scope, "delta": "已接收"},
            {"type": f"response.{kind}.done", **scope, "text": "另一段内容"},
        ], deltas)

    assert getattr(caught.value, "response_delta_seen", False) is True
    assert caught.value.retryable is False
    assert len(deltas) == 1


@pytest.mark.asyncio
async def test_single_part_without_identity_metadata_remains_supported():
    deltas = []

    result = await consume([
        {"type": "response.output_text.delta", "delta": "你"},
        {"type": "response.output_text.done", "text": "你好"},
        {"type": "response.output_text.done", "text": "你好"},
    ], deltas)

    assert result.content == "你好"
    assert deltas == [{"content_delta": "你"}, {"content_delta": "好"}]
