"""Codex must accept the immutable messages and tools in a ModelRequest."""

import json
from dataclasses import replace

import httpx
import pytest

from agent.plugin_composition import ModelRequest
from core.net.http import HttpClient
from plugins.codex.responses import CodexResponses
from tests.model_plugin_fakes import BoundChatModelFake


@pytest.mark.asyncio
@pytest.mark.parametrize("lite", [False, True])
async def test_frozen_request_can_be_estimated_and_sent(lite):
    messages = [{"role": "user", "content": [{"type": "text", "text": "你好"}]}]
    schema = {
        "type": "object",
        "properties": {"city": {"type": "string", "enum": ["上海", "北京"]}},
        "required": ["city"],
    }
    tools = [{"type": "function", "function": {"name": "weather", "parameters": schema}}]
    request = ModelRequest(messages=messages, tools=tools)
    sent = []

    def handle(wire_request):
        sent.append(json.loads(wire_request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text='data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
                 'data: {"type":"response.completed","response":{}}\n\n',
        )

    class Credential:
        async def read(self):
            return {
                "driver": "codex", "access_token": "fixture", "account_id": "test",
                "expires_at": "2099-01-01T00:00:00+00:00",
            }

    http = HttpClient(lambda: httpx.AsyncClient(
        base_url="https://codex.invalid", transport=httpx.MockTransport(handle),
    ))
    driver = CodexResponses(
        http=http,
        credential=Credential(),
        descriptor=replace(BoundChatModelFake(object()).descriptor, driver_id="codex"),
        config={"use_responses_lite": lite},
    )
    try:
        assert driver.estimate_context_tokens(request.messages, request.tools) == max(
            1, len(json.dumps([messages, tools], ensure_ascii=False)) // 4,
        )
        assert driver.estimate_appended_message_tokens(request.messages) == max(
            1, len(json.dumps(messages, ensure_ascii=False)) // 4,
        )
        assert (await driver.complete(request)).content == "ok"
        assert len(sent) == 1
        wire_tools = sent[0]["input"][0]["tools"] if lite else sent[0]["tools"]
        assert wire_tools[0]["parameters"] == schema
        assert sent[0]["input"][-1]["content"] == [{"type": "input_text", "text": "你好"}]
    finally:
        await http.aclose()
