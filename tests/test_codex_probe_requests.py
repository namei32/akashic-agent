"""Setup probes must retain the required Responses Lite transport context."""

import json

import httpx
import pytest

from agent.plugin_composition import (
    BoundModelDescriptor,
    CapabilitySources,
    ModelCapabilities,
    ModelRequest,
)
from core.net.http import HttpClient
from plugins.codex.responses import CodexResponses


@pytest.mark.asyncio
@pytest.mark.parametrize("lite", [False, True])
@pytest.mark.parametrize("disable_reasoning", [False, True])
async def test_probe_keeps_required_transport_context(lite, disable_reasoning):
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        reasoning = payload.get("reasoning", {})
        if request.headers.get("x-openai-internal-codex-responses-lite") == "true":
            if reasoning.get("context") != "all_turns":
                return httpx.Response(400, json={"error": {
                    "message": "Responses Lite requires reasoning.context=all_turns",
                    "param": "reasoning.context", "code": "unsupported_value",
                }})
        else:
            assert "context" not in reasoning
        if disable_reasoning:
            assert "effort" not in reasoning
            assert "summary" not in reasoning
        else:
            assert reasoning["effort"] == "low"
            assert reasoning["summary"] == "auto"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=(
            'data: {"type":"response.output_text.delta","delta":"OK"}\n\n'
            'data: {"type":"response.completed","response":{}}\n\n'
        ))

    class Credential:
        async def read(self):
            return {
                "driver": "codex", "access_token": "fixture", "account_id": "test",
                "expires_at": "2099-01-01T00:00:00+00:00",
            }

    http = HttpClient(lambda: httpx.AsyncClient(
        base_url="https://codex.invalid", transport=httpx.MockTransport(respond),
    ))
    driver = CodexResponses(
        http=http,
        credential=Credential(),
        descriptor=BoundModelDescriptor(
            binding_id="probe-binding", plugin_snapshot_id="probe-snapshot",
            model_revision=1, model_id="probe-model", connection_id="probe-connection",
            driver_id="codex", driver_contract_version="1", auth_identity="test",
            model="test-model", role="agent", reasoning_effort="low",
            capabilities=ModelCapabilities(), capability_sources=CapabilitySources(),
            capability_digest="probe-capabilities",
        ),
        config={"use_responses_lite": lite, "reasoning_summary": "auto"},
    )
    try:
        # This is the request shape used by Models when saving a chat model.
        response = await driver.complete(ModelRequest(
            messages=({"role": "user", "content": "Reply OK."},),
            max_output_tokens=256,
            disable_reasoning=disable_reasoning,
        ))
        assert response.content == "OK"
        assert len(requests) == 1
    finally:
        await http.aclose()
