"""Replay historical conversations through the public append-only MessageLog API."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from typing import Callable

from plugins.content.plugin import check_text
from session.log import SessionAttributes
from session.message import ContentPart, Input, Output, Control
from .runtime import BenchmarkRuntime, parse_date, replay_clock


async def ingest_instance(rt: BenchmarkRuntime, instance, *, force: bool = False,
                          on_progress: Callable[[int, int], None] | None = None) -> int:
    if rt.started:
        raise RuntimeError("Historical import must finish before runtime consumers start")
    state_path = rt.workspace / "ingest_state.json"
    raw = [(instance.haystack_dates[i] if i < len(instance.haystack_dates) else '',
            [(turn.role, turn.content) for turn in turns])
           for i, turns in enumerate(instance.haystack_sessions)]
    digest = hashlib.sha256(json.dumps(raw, ensure_ascii=False).encode()).hexdigest()
    if state_path.exists():
        old = json.loads(state_path.read_text())
        if old.get('source_digest') != digest:
            raise ValueError("Historical input changed; use a new case workspace")
        if old.get('completed') and not force:return 0
    expected = sum(len(turns) for turns in instance.haystack_sessions)
    state = dict(version=2, question_id=instance.question_id, source_digest=digest,
                 completed=False, expected_turns=expected, ingested_turns=0)
    state_path.write_text(json.dumps(state, indent=2))
    records = []
    for i, turns in enumerate(instance.haystack_sessions):
        date = instance.haystack_dates[i] if i < len(instance.haystack_dates) else ''
        stamp = parse_date(date, fallback=datetime(2020, 1, 1, tzinfo=UTC) + timedelta(seconds=i))
        records.append((stamp, i, turns))
    total = 0
    log = rt.core.message_log
    for done, (stamp, index, turns) in enumerate(sorted(records, key=lambda x: (x[0], x[1])), 1):
        session_id = f"{instance.session_key}:history:{index}"
        log.ensure_session(session_id, SessionAttributes("internal", "eligible"))
        user = log.writer(session_id, author="user", source="conversation", body_types=(Input,), content={"text":check_text})
        assistant = log.writer(session_id, author="assistant", source="conversation", body_types=(Output,), content={"text":check_text})
        try:
            last = None
            for position, turn in enumerate(turns):
                if turn.role not in {"user", "assistant"}:
                    raise ValueError(f"Unsupported history role: {turn.role}")
                with replay_clock(stamp + timedelta(microseconds=position)):
                    parts = (ContentPart("text", turn.content),)
                    identity = f"{instance.question_id}:history:{index}:{position}"
                    last = (user.append(identity, Input(parts)) if turn.role == "user"
                            else assistant.append(identity, Output(parts, "complete")))
                total += 1
            # An unanswered source input must not trigger a new model reply on replay.
            if last is not None and isinstance(last.body, Input):
                control = log.writer(session_id, author="benchmark", source="conversation", body_types=(Control,), content={})
                with replay_clock(stamp + timedelta(microseconds=len(turns))):
                    control.append(f"{instance.question_id}:history:{index}:pause", Control("pause", last.seq))
                control.expire()
        finally:
            user.expire()
            assistant.expire()
        if on_progress:on_progress(done, len(records))
    replay_end = max((stamp + timedelta(microseconds=len(turns)) for stamp, _, turns in records),
                     default=datetime(2020, 1, 1, tzinfo=UTC))
    state.update(completed=True, ingested_turns=total, replay_end=replay_end.isoformat(),
                 timestamp_policy="source timestamps; missing dates use a monotonic synthetic 2020 clock")
    state_path.write_text(json.dumps(state, indent=2))
    return total
