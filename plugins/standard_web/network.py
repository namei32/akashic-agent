"""Optional per-plugin source interface for hosts with a broken proxy route."""
from __future__ import annotations

import ipaddress
import subprocess
import sys
from typing import TypedDict

import httpx
from pydantic import BaseModel, ConfigDict, Field


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_interface: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]+$")


class ClientOptions(TypedDict, total=False):
    transport: httpx.AsyncHTTPTransport
    trust_env: bool


def client_options(config: Config) -> ClientOptions:
    if config.source_interface is None:
        return {}
    if sys.platform != "darwin":
        raise RuntimeError("standard_web source_interface currently requires macOS")
    result = subprocess.run(
        ["/usr/sbin/ipconfig", "getifaddr", config.source_interface],
        capture_output=True, text=True, timeout=3, check=False,
    )
    address = result.stdout.strip()
    if result.returncode or not address:
        raise RuntimeError(f"Network interface {config.source_interface} has no IPv4 address")
    ip = ipaddress.IPv4Address(address)
    if ip.is_loopback or ip.is_unspecified:
        raise ValueError("source_interface must have a non-loopback IPv4 address")
    # Resolve at each new client, so Wi-Fi address changes do not pin a stale IP.
    return {"transport": httpx.AsyncHTTPTransport(local_address=str(ip)), "trust_env": False}
