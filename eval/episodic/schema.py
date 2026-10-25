from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Evidence(Record):
    id: str
    session_id: str
    speaker: str
    role: Literal["user", "assistant", "observation"]
    at: AwareDatetime
    text: str

    @property
    def embedding_text(self) -> str:
        return (
            f"{self.speaker}: {self.text}" if self.role == "observation" else self.text
        )


class Unit(Record):
    id: str
    messages: tuple[Evidence, ...] = Field(min_length=1)
    feedback: tuple[Any, ...] = ()

    @property
    def input_text(self) -> str:
        return "\n".join(
            m.embedding_text for m in self.messages if m.role != "assistant"
        )

    @property
    def output_text(self) -> str:
        return "\n".join(m.text for m in self.messages if m.role == "assistant")


class Corpus(Record):
    id: str
    adapter: str
    units: tuple[Unit, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def check(self):
        if any(unit.feedback for unit in self.units):
            raise ValueError(
                "EpBench/InMind do not supply hidden annotation-derived memory feedback"
            )
        ids = [m.id for u in self.units for m in u.messages]
        ends = [max(m.at for m in u.messages) for u in self.units]
        if len(ids) != len(set(ids)) or len({u.id for u in self.units}) != len(
            self.units
        ):
            raise ValueError("Duplicate original evidence identity")
        if ends != sorted(ends) or any(
            len({m.session_id for m in u.messages}) != 1 for u in self.units
        ):
            raise ValueError("Corpus must retain ordered, session-local units")
        return self

    def visible(self, cutoff):
        if any(m.at > cutoff for u in self.units for m in u.messages):
            raise ValueError("This suite requires the complete frozen history")
        return self


class Case(Record):
    id: str
    dataset: Literal["epbench", "inmind"]
    corpus_id: str
    cluster_id: str
    split: Literal["dev", "holdout"]
    category: str
    question: str = Field(min_length=1)
    cutoff: AwareDatetime
    gold_ids: tuple[str, ...] = ()
    data_issues: tuple[str, ...] = ()
    # Annotation fields are used only for judging and diagnostic controls.
    truth: dict[str, Any]


class Suite(Record):
    version: Literal[1] = 1
    source_files: dict[str, str]
    protocol: dict[str, Any]
    corpora: tuple[Corpus, ...]
    cases: tuple[Case, ...]

    @model_validator(mode="after")
    def check(self):
        histories = {c.id: c for c in self.corpora}
        if len(histories) != len(self.corpora) or len(
            {c.id for c in self.cases}
        ) != len(self.cases):
            raise ValueError("Duplicate corpus or case identity")
        splits: dict[str, set[str]] = {}
        for case in self.cases:
            corpus = histories[case.corpus_id]
            known = {m.id for u in corpus.units for m in u.messages}
            if not set(case.gold_ids) <= known or any(
                m.at > case.cutoff for u in corpus.units for m in u.messages
            ):
                raise ValueError("Question evidence is outside its frozen history")
            splits.setdefault(case.cluster_id, set()).add(case.split)
        if any(len(values) > 1 for values in splits.values()):
            raise ValueError("A history cluster crosses development and holdout")
        return self


def clock(index: int) -> datetime:
    """Acceptance order only; dates of narrative events remain in original text."""
    return datetime(2000, 1, 1, tzinfo=UTC) + timedelta(seconds=index)


def canonical(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(canonical(value) + "\n")


def jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def verify_sources(suite: Suite) -> None:
    for name, expected in suite.source_files.items():
        if file_digest(Path(name)) != expected:
            raise ValueError(f"Frozen dataset or scoring source changed: {name}")
