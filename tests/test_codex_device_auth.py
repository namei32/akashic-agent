"""Provider-owned Codex login input, including older models UI compatibility."""

import base64
import json

import httpx
import pytest

from plugins.codex import auth


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_identity", [None, "codex-draft-connection", "previous-account"])
@pytest.mark.parametrize("wire_interval", [5, "5"])
async def test_device_login_uses_authorized_account_not_ui_identity(monkeypatch, legacy_identity, wire_interval):
    claims = {"https://api.openai.com/auth": {"chatgpt_account_id": "authorized-account"}}
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/api/accounts/deviceauth/usercode":
            assert json.loads(request.content) == {"client_id": auth.CODEX_CLIENT_ID}
            return httpx.Response(200, json={
                "user_code": "fixture-code", "device_auth_id": "fixture-device", "interval": wire_interval,
            })
        if request.url.path == "/api/accounts/deviceauth/token":
            assert json.loads(request.content) == {
                "device_auth_id": "fixture-device", "user_code": "fixture-code",
            }
            return httpx.Response(200, json={
                "authorization_code": "fixture-authorization", "code_verifier": "fixture-verifier",
            })
        assert request.url.path == "/oauth/token"
        return httpx.Response(200, json={
            "access_token": "fixture-access", "refresh_token": "fixture-refresh",
            "id_token": f"fixture.{encoded}.fixture", "expires_in": "3600" if isinstance(wire_interval, str) else 3600,
        })

    client_type = httpx.AsyncClient
    monkeypatch.setattr(auth.httpx, "AsyncClient", lambda **kwargs: client_type(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    input = {"auth_base": "https://login.invalid", "api_base": "https://codex.invalid"}
    if legacy_identity is not None:
        input["auth_identity"] = legacy_identity
    started = await auth.start_auth(input)
    assert "auth_identity" not in started["state"]
    assert "auth_identity" not in started["challenge"]
    assert started["challenge"]["interval"] == 5
    finished = await auth.finish_auth(started["state"])
    assert finished["status"] == "complete"
    assert finished["auth_identity"] == "authorized-account"
    assert finished["credential"]["account_id"] == "authorized-account"
    assert len(requests) == 3


@pytest.mark.asyncio
async def test_device_login_still_rejects_unsupported_fields_before_http(monkeypatch):
    def unexpected_client(**kwargs):
        pytest.fail("unsupported input must be rejected before opening an HTTP client")

    monkeypatch.setattr(auth.httpx, "AsyncClient", unexpected_client)
    with pytest.raises(ValueError, match="unsupported Codex auth input: account_id"):
        await auth.start_auth({"account_id": "untrusted-account"})


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", [True, 5.5, "5.5", "five", "５", "5" * 21])
async def test_device_login_rejects_malformed_interval(monkeypatch, interval):
    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "user_code": "fixture-code", "device_auth_id": "fixture-device", "interval": interval,
    }))
    monkeypatch.setattr(auth.httpx, "AsyncClient", lambda **kwargs: client_type(
        transport=transport, **kwargs,
    ))
    with pytest.raises(auth.AuthenticationError, match="无效整数"):
        await auth.start_auth({})
