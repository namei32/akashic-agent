import subprocess

import pytest

from plugins.standard_web import network


def test_default_keeps_existing_http_routing(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Default networking must not inspect an interface")
    monkeypatch.setattr(network.subprocess, "run", unexpected)
    assert network.client_options(network.Config()) == {}


def test_resolves_changed_interface_for_each_client(monkeypatch):
    addresses = iter(["192.168.31.10\n", "192.168.31.11\n"])
    observed = []
    monkeypatch.setattr(network.sys, "platform", "darwin")
    def resolve(argv, **kwargs):
        assert argv == ["/usr/sbin/ipconfig", "getifaddr", "en0"]
        return subprocess.CompletedProcess(argv, 0, next(addresses), "")
    monkeypatch.setattr(network.subprocess, "run", resolve)
    def transport(**kwargs):
        observed.append(kwargs["local_address"])
        return object()
    monkeypatch.setattr(network.httpx, "AsyncHTTPTransport", transport)
    config = network.Config(source_interface="en0")
    assert network.client_options(config)["trust_env"] is False
    assert network.client_options(config)["trust_env"] is False
    assert observed == ["192.168.31.10", "192.168.31.11"]


@pytest.mark.parametrize("value,code", [("", 1), ("invalid", 0), ("127.0.0.1", 0)])
def test_invalid_interface_does_not_fall_back_to_broken_proxy(monkeypatch, value, code):
    monkeypatch.setattr(network.sys, "platform", "darwin")
    monkeypatch.setattr(network.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, code, value, ""))
    with pytest.raises((RuntimeError, ValueError)):
        network.client_options(network.Config(source_interface="en0"))
