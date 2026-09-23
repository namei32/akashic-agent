from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Evidence(Record):
    id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    source_session_id: str | None = None
    speaker: str = Field(min_length=1)
    role: Literal["user", "assistant", "observation"]
    at: AwareDatetime
    text: str

    @property
    def embedding_text(self) -> str:
        if not self.text.strip():
            return ""
        return f"{self.speaker}: {self.text}" if self.role == "observation" else self.text


class Unit(Record):
    id: str
    messages: tuple[Evidence, ...] = Field(min_length=1)

    @property
    def input_text(self) -> str:
        return "\n".join(m.embedding_text for m in self.messages if m.role != "assistant" and m.embedding_text)

    @property
    def output_text(self) -> str:
        return "\n".join(m.embedding_text for m in self.messages if m.role == "assistant" and m.embedding_text)


class Corpus(Record):
    id: str
    adapter: str
    units: tuple[Unit, ...]

    @model_validator(mode="after")
    def check(self):
        ids = [m.id for u in self.units for m in u.messages]
        if len(ids) != len(set(ids)) or len({u.id for u in self.units}) != len(self.units):
            raise ValueError("Duplicate evidence or unit identity")
        if any(len({m.session_id for m in u.messages}) != 1 for u in self.units):
            raise ValueError("A unit cannot cross source sessions")
        ends = [max(m.at for m in u.messages) for u in self.units]
        if ends != sorted(ends):
            raise ValueError("History units must be chronological")
        return self

    def visible(self, cutoff) -> Corpus:
        # Never expose half of a not-yet-completed historical unit.
        return self.model_copy(update={
            "units": tuple(u for u in self.units if all(m.at <= cutoff for m in u.messages))
        })


class Case(Record):
    id: str
    dataset: Literal["longmemeval", "locomo"]
    corpus_id: str
    cluster_id: str
    split: Literal["dev", "holdout"]
    category: str
    question: str = Field(min_length=1)
    cutoff: AwareDatetime
    question_at: AwareDatetime | None = None
    answer: str
    unanswerable: bool = False
    gold_ids: tuple[str, ...] = ()
    gold_level: Literal["message", "session", "none"] = "none"
    official_category: int | None = None
    data_issues: tuple[str, ...] = ()


class Suite(Record):
    version: Literal[1] = 1
    sources: dict[str, str]
    corpora: tuple[Corpus, ...]
    cases: tuple[Case, ...]

    @model_validator(mode="after")
    def check(self):
        corpora = {c.id: c for c in self.corpora}
        if len(corpora) != len(self.corpora) or len({c.id for c in self.cases}) != len(self.cases):
            raise ValueError("Duplicate corpus or question identity")
        groups: dict[tuple[str, str], set[str]] = {}
        for case in self.cases:
            if case.corpus_id not in corpora:
                raise ValueError("Question references an unknown history")
            messages = [m for u in corpora[case.corpus_id].visible(case.cutoff).units for m in u.messages]
            known = {m.id if case.gold_level == "message" else (m.source_session_id or m.session_id) for m in messages}
            if not set(case.gold_ids) <= known:
                if "gold_after_question_time" not in case.data_issues:
                    raise ValueError(f"Gold evidence absent at query time: {case.id}")
            if case.gold_level == "none" and case.gold_ids:
                raise ValueError("Unspecified evidence level has labels")
            groups.setdefault((case.dataset, case.cluster_id), set()).add(case.split)
        if any(len(splits) > 1 for splits in groups.values()):
            raise ValueError("A history cluster crosses development and holdout")
        return self


class Settings(Record):
    model: str = "gpt-5.6-sol"
    effort: str = "max"
    embedding_model: str = "text-embedding-v4"
    dimensions: int = Field(default=1024, gt=0)
    budget_chars: int = Field(default=12000, ge=64)
    candidate_limit: int = Field(default=100, gt=0)
    rrf_k: int = Field(default=60, gt=0)
    full_limit: int = Field(default=40, ge=1, le=40)
    repeats: int = Field(default=3, ge=1)
    output_tokens: int = Field(default=4096, gt=0)
    timeout_s: float = Field(default=300, gt=0)
    seed: int = 20260923


def canonical(value) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path: Path, value) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(canonical(value) + "\n")


def read_suite(path: Path) -> Suite:
    return Suite.model_validate_json(path.read_text(encoding="utf-8"))
