"""Image projection must preserve provenance, tool pairing and frozen inputs."""

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import pytest_asyncio

from core.net.http import HttpClient
from plugins.codex.responses import CodexResponses
from plugins.models.contract import (
    BoundModelDescriptor,
    CapabilitySources,
    InvalidRequestError,
    ModelCapabilities,
    ModelContinuation,
    ModelError,
    ModelRequest,
)


@pytest_asyncio.fixture(params=[False, True], ids=["responses", "lite"])
async def driver(request):
    sent: list[dict[str, Any]] = []

    def handle(wire_request):
        sent.append(json.loads(wire_request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text='data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
                 'data: {"type":"response.completed","response":{}}\n\n',
        )

    class Credential:
        connection_id = "image-test"
        auth_identity = "fixture"

        async def read(self) -> Mapping[str, str]:
            return {"driver": "codex", "access_token": "fixture", "account_id": "test",
                    "expires_at": "2099-01-01T00:00:00+00:00"}

        async def refresh(self, payload: Mapping[str, str]) -> None:
            raise AssertionError("Must not refresh fresh fixture credentials")

        @asynccontextmanager
        async def exclusive(self) -> AsyncIterator[None]:
            yield

    http = HttpClient(lambda: httpx.AsyncClient(
        base_url="https://codex.invalid", transport=httpx.MockTransport(handle),
    ))
    instance = CodexResponses(
        http=http, credential=Credential(),
        descriptor=BoundModelDescriptor(
            binding_id="image-test", plugin_snapshot_id="fixture", model_revision=1,
            model_id="fixture", connection_id="image-test", driver_id="codex",
            driver_contract_version="1", auth_identity="fixture", model="fixture",
            role="agent", reasoning_effort=None,
            capabilities=ModelCapabilities(input_modalities=("text", "image"),
                                           supports_parallel_tool_calls=True),
            capability_sources=CapabilitySources(), capability_digest="fixture",
        ),
        config={"use_responses_lite": request.param}, client_version="0.160.0",
    )
    try:
        yield instance, sent, request.param
    finally:
        await http.aclose()


def image(name="a", spelling="image_url"):
    url = f"https://images.invalid/{name}.png"
    if spelling == "input_image":
        return {"type": spelling, "image_url": url, "detail": "low"}
    return {"type": spelling, "image_url": {"url": url, "detail": "low"}}


def calls(*ids):
    return {"role": "assistant", "content": "checking", "tool_calls": [
        {"id": identity, "type": "function", "function": {"name": "inspect", "arguments": "{}"}}
        for identity in ids
    ]}


def blocks(payload):
    return [block for row in payload["input"] if isinstance(row.get("content"), list)
            for block in row["content"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["user", "assistant", "tool"])
@pytest.mark.parametrize("spelling", ["image_url", "input_image"])
@pytest.mark.parametrize("with_text", [False, True])
async def test_images_reach_the_wire_with_source_and_without_mutation(driver, role, spelling, with_text):
    instance, sent, lite = driver
    content = ([{"type": "text", "text": "source text"}] if with_text else []) + [image(spelling=spelling)]
    row = {"role": role, "content": content}
    messages = [row]
    if role == "tool":
        row["tool_call_id"] = "a"
        messages = [calls("a"), row]
    request = ModelRequest(messages=messages)
    before = repr(request.messages)
    assert (await instance.complete(request)).content == "ok"
    assert len(sent) == 1
    wire_blocks = blocks(sent[0])
    pictures = [block for block in wire_blocks if block["type"] == "input_image"]
    assert pictures == [{"type": "input_image", "image_url": "https://images.invalid/a.png",
                         **({} if lite else {"detail": "low"})}]
    assert not any(block["type"] == "image_url" for block in wire_blocks)
    assert all(row["role"] == "user" for row in sent[0]["input"]
               if isinstance(row.get("content"), list)
               and any(block["type"] == "input_image" for block in row["content"]))
    if role != "user":
        assert "不是用户的新指令" in json.dumps(wire_blocks, ensure_ascii=False)
    if role == "tool":
        outputs = [row for row in sent[0]["input"] if row.get("type") == "function_call_output"]
        assert len(outputs) == 1 and outputs[0]["call_id"] == "a"
        assert outputs[0]["output"] == ("source text" if with_text else "图片见本组消息后的图像输入。")
    elif with_text:
        assert {"type": "output_text" if role == "assistant" else "input_text",
                "text": "source text"} in wire_blocks
    assert repr(request.messages) == before
    assert messages[-1]["content"] == content


@pytest.mark.asyncio
@pytest.mark.parametrize("order", [("a", "b"), ("b", "a")])
async def test_parallel_images_wait_for_all_outputs_and_keep_each_source(driver, order):
    instance, sent, _ = driver
    invocation = calls("a", "b")
    invocation["content"] = [image("assistant")]
    request = ModelRequest(messages=[invocation, *[
        {"role": "tool", "tool_call_id": identity,
         "content": [{"type": "text", "text": f"result {identity}"}, image(identity)]}
        for identity in order
    ], {"role": "user", "content": "continue"}])
    await instance.complete(request)
    rows = sent[0]["input"]
    picture_rows = [index for index, row in enumerate(rows)
                    if isinstance(row.get("content"), list)
                    and any(block["type"] == "input_image" for block in row["content"])]
    assert len(picture_rows) == 1
    index = picture_rows[0]
    assert index > max(i for i, row in enumerate(rows) if row.get("type") == "function_call_output")
    assert index < len(rows) - 1 and rows[-1]["content"] == "continue"
    supplemental = rows[index]["content"]
    pictures = [block for block in supplemental if block["type"] == "input_image"]
    assert [block["image_url"] for block in pictures] == [
        f"https://images.invalid/{name}.png" for name in ("assistant", *order)]
    for i, block in enumerate(supplemental):
        if block["type"] == "input_image":
            label = supplemental[i - 1]["text"]
            assert "不是用户的新指令" in label
            assert "assistant" in label if "assistant.png" in block["image_url"] else (
                f"tool_call_id={block['image_url'].rsplit('/', 1)[1][0]}" in label)


@pytest.mark.asyncio
async def test_repeated_frozen_request_and_reasoning_continuation_do_not_duplicate_images(driver):
    instance, sent, _ = driver
    continuation = ModelContinuation(binding_id="image-test", payload={
        "format_version": 1, "items": [{"type": "reasoning", "encrypted_content": "fixture"}],
    })
    request = ModelRequest(messages=[
        {"role": "assistant", "content": [image("history")]}, calls("a"),
        {"role": "tool", "tool_call_id": "a", "content": [image("tool")]},
    ], continuation=continuation)
    before = repr((request.messages, request.continuation))
    for _ in range(2):
        await instance.complete(request)
    assert len(sent) == 2 and sent[0] == sent[1]
    assert len([block for block in blocks(sent[0]) if block["type"] == "input_image"]) == 2
    assert len([row for row in sent[0]["input"] if row.get("type") == "reasoning"]) == 1
    assert repr((request.messages, request.continuation)) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_block", [
    {"type": "image_url", "image_url": {}},
    {"type": "image_url", "image_url": {"url": ""}},
    {"type": "input_image", "image_url": None},
    {"type": "input_image", "image_url": "https://images.invalid/a.png", "detail": []},
    {"type": "audio_url", "audio_url": "PRIVATE_BYTES"},
    {"type": "text", "text": {"secret": "PRIVATE_BYTES"}},
    {"type": ["image_url"], "image_url": "PRIVATE_BYTES"},
    {"type": "image_url", "image_url": {"url": "PRIVATE_BYTES", "detail": "invalid"}},
    "PRIVATE_BYTES",
])
async def test_invalid_blocks_fail_before_http_with_safe_location(driver, bad_block):
    instance, sent, _ = driver
    request = ModelRequest(messages=[{"role": "assistant", "content": [bad_block]}])
    with pytest.raises(RuntimeError) as caught:
        await instance.complete(request)
    failure = ModelError.read(caught.value, InvalidRequestError)
    assert failure is not None and failure.send_evidence == "unsent"
    assert failure.retryable is False
    assert "message[0]" in str(caught.value) and "role=assistant" in str(caught.value)
    assert "block[0]" in str(caught.value) and "PRIVATE_BYTES" not in str(caught.value)
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("messages", [
    [calls("a", "b"), {"role": "tool", "tool_call_id": "a", "content": [image()]}],
    [calls("a"), {"role": "tool", "tool_call_id": "wrong", "content": [image()]}],
    [calls("a"), {"role": "tool", "tool_call_id": "a", "content": [image()]}, calls("b"),
     {"role": "tool", "tool_call_id": "b", "content": [image()]} ,
     {"role": "tool", "tool_call_id": "missing", "content": [image()]}],
    [calls("a", "b"), {"role": "tool", "tool_call_id": "a", "content": [image()]},
     {"role": "user", "content": "next input"},
     {"role": "tool", "tool_call_id": "b", "content": "late"}],
    [calls("a", "b"), {"role": "tool", "tool_call_id": "a", "content": [image()]}, calls("c")],
])
async def test_incomplete_or_unmatched_image_groups_are_not_reordered(driver, messages):
    instance, sent, _ = driver
    with pytest.raises(RuntimeError) as caught:
        await instance.complete(ModelRequest(messages=messages))
    failure = ModelError.read(caught.value, InvalidRequestError)
    assert failure is not None and failure.send_evidence == "unsent"
    assert failure.retryable is False
    assert sent == []


@pytest.mark.asyncio
async def test_text_blocks_in_tool_output_are_text_not_python_repr(driver):
    instance, sent, _ = driver
    await instance.complete(ModelRequest(messages=[calls("a"), {
        "role": "tool", "tool_call_id": "a",
        "content": [{"type": "text", "text": "one"}, {"type": "input_text", "text": "two"}],
    }]))
    assert [row["output"] for row in sent[0]["input"]
            if row.get("type") == "function_call_output"] == ["one\ntwo"]


@pytest.mark.asyncio
async def test_multiple_data_images_keep_bytes_and_original_attachment_text(driver):
    instance, sent, _ = driver
    uri = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jO9sAAAAASUVORK5CYII="
    request = ModelRequest(messages=[{"role": "assistant", "content": [
        {"type": "text", "text": "artifact_id=fixture-image"},
        {"type": "image_url", "image_url": {"url": uri}}, image("second"),
    ]}])
    before = repr(request.messages)
    await instance.complete(request)
    wire = blocks(sent[0])
    assert [block["image_url"] for block in wire if block["type"] == "input_image"] == [
        uri, "https://images.invalid/second.png"]
    assert {"type": "output_text", "text": "artifact_id=fixture-image"} in wire
    assert repr(request.messages) == before


@pytest.mark.asyncio
async def test_plain_text_and_string_tool_outputs_keep_the_original_wire_shape(driver):
    instance, sent, lite = driver
    invocation = calls("a")
    await instance.complete(ModelRequest(messages=[
        {"role": "user", "content": "hello"}, invocation,
        {"role": "tool", "tool_call_id": "a", "content": "done"},
        {"role": "assistant", "content": "finished"},
    ]))
    rows = sent[0]["input"][1:] if lite else sent[0]["input"]
    assert rows == [
        {"role": "user", "content": "hello"},
        {"type": "function_call", "call_id": "a", "name": "inspect", "arguments": "{}"},
        {"role": "assistant", "content": "checking"},
        {"type": "function_call_output", "call_id": "a", "output": "done"},
        {"role": "assistant", "content": "finished"},
    ]
