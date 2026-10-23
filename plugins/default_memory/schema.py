"""Canonical facts and source receipts, independent of message storage."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

Kind = Literal["profile", "preference", "procedure", "event"]


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Kind
    summary: str = Field(min_length=1, max_length=2000)
    category: str = Field(default="", max_length=80)
    emotional_weight: float = Field(default=0, ge=0, le=10)
    tool_requirement: str = Field(default="", max_length=200)
    steps: tuple[str, ...] = Field(default=(), max_length=12)
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=40)

    @field_validator("summary")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Memory summary cannot be blank")
        return value.strip()


class Retraction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    memory_id: str
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=500)


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    facts: tuple[Fact, ...] = Field(default=(), max_length=24)
    retract: tuple[Retraction, ...] = Field(default=(), max_length=12)


@dataclass(frozen=True, slots=True)
class Source:
    key: str
    graph: str
    session_id: str
    ending_id: str
    timestamp: str
    message_ids: tuple[str, ...]
    user_ids: tuple[str, ...]
    digest: str
    text: str


@dataclass(frozen=True, slots=True)
class Memory:
    id: str
    graph: str
    fact: Fact
    vector: tuple[float, ...]
    reinforcement: int
    created_at: str
    updated_at: str
    status: str
    source_key: str
