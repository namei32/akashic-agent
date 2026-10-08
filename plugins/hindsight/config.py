from __future__ import annotations

import hashlib
import json
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent.plugin_composition.messages import SessionAttributes


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = False
    base_url: str = "http://127.0.0.1:8888"
    tenant: str = "default"
    bank_prefix: str = "akashic"
    ledger_namespace: str = "default"
    api_key_env: str = "HINDSIGHT_API_KEY"
    sources: tuple[str, ...] = ("conversation", "programmatic", "legacy-unattributed")
    scope_policies: dict[str, dict[str, Literal["global", "isolated", "off"]]] = Field(
        default_factory=dict
    )
    timeout_seconds: float = Field(default=15, gt=0, le=300)
    poll_seconds: float = Field(default=5, gt=0, le=300)
    batch_size: int = Field(default=32, ge=1, le=100)
    budget: Literal["low", "mid", "high"] = "mid"
    max_tokens: int = Field(default=4096, ge=1, le=32000)
    max_chars: int = Field(default=12000, ge=200, le=128000)
    recall_limit: int = Field(default=40, ge=1, le=40)
    feedback_enabled: bool = True

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "Hindsight URL 必须是无凭据、query 或 fragment 的 HTTP(S) 地址"
            )
        return value.rstrip("/")

    @field_validator("tenant", "bank_prefix", "ledger_namespace")
    @classmethod
    def validate_identity(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", value):
            raise ValueError("Hindsight tenant/bank_prefix 必须是安全的稳定身份")
        return value

    @field_validator("api_key_env")
    @classmethod
    def validate_env(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("API key 环境变量名称无效")
        return value

    def storage_identity(self) -> str:
        """Changes to audience or backend require a new ledger/bank namespace."""
        return digest(
            {
                "version": 1,
                "base_url": self.base_url,
                "tenant": self.tenant,
                "bank_prefix": self.bank_prefix,
                "sources": self.sources,
                "scope_policies": self.scope_policies,
            }
        )

    def route(self, attributes: SessionAttributes) -> tuple[str, bool]:
        policies = [
            (name, value, self.scope_policies.get(name, {}).get(value, "global"))
            for name, value in attributes.scope
        ]
        isolated = [
            (name, value) for name, value, policy in policies if policy == "isolated"
        ]
        bank = self.bank_prefix + (
            "-" + digest(isolated)[:24] if isolated else "-default"
        )
        learn = attributes.learning == "eligible" and all(
            policy != "off" for _, _, policy in policies
        )
        return bank, learn


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
