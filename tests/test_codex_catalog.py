"""Catalog negotiation must expose current models and preserve provider filters."""

import httpx
import pytest

from agent.plugin_composition import DriverConnectionDescriptor
from plugins.codex import catalog


class Credential:
    connection_id = "fixture-connection"
    auth_identity = "fixture-account"

    async def read(self):
        return {
            "driver": "codex", "access_token": "fixture-access",
            "account_id": self.auth_identity, "expires_at": "2099-01-01T00:00:00+00:00",
        }


def descriptor():
    return DriverConnectionDescriptor(
        connection_id="fixture-connection", name="Fixture", driver_id="codex",
        endpoint="https://codex.invalid", auth_identity="fixture-account", config={},
    )


def visible_model(slug):
    return {
        "slug": slug, "visibility": "list", "supported_in_api": True,
        "context_window": 272000, "input_modalities": ["text", "image"],
        "supported_reasoning_levels": [{"effort": x} for x in ["low", "medium", "max", "ultra"]],
        "default_reasoning_level": "low", "supports_parallel_tool_calls": True,
    }


@pytest.mark.asyncio
async def test_catalog_negotiates_current_models_instead_of_legacy_catalog(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.path == "/models"
        assert request.headers["ChatGPT-Account-ID"] == "fixture-account"
        current = request.url.params.get("client_version") == "0.160.0"
        models = [visible_model("gpt-5.6-sol")]
        if current:
            models.insert(0, visible_model("gpt-6.1-sol"))
        models.extend([
            {**visible_model("gpt-reserve"), "visibility": "hide"},
            {**visible_model("fixture-no-api"), "supported_in_api": False},
        ])
        return httpx.Response(200, json={"models": models})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(catalog.httpx, "AsyncClient", lambda **kwargs: client_type(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    models = await catalog.discover(descriptor(), Credential())
    assert [model.model for model in models] == ["gpt-6.1-sol", "gpt-5.6-sol"]
    assert models[0].capabilities.input_modalities == ("text", "image")
    assert models[0].capabilities.supported_reasoning_efforts == ("low", "medium", "max", "ultra")
    assert models[0].default_reasoning_effort == "low"
    assert len(requests) == 1
