"""Opt-in, same-scope Web conversation selection; never changes delivery routing."""
from __future__ import annotations

from collections import defaultdict
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import json
from typing import TYPE_CHECKING, AsyncIterator

if TYPE_CHECKING:
    from .api import DeliveryTarget
    from .request import Request

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.plugin_composition import Context
from agent.plugin_composition.plugin_config import PLUGIN_CONFIG
from core.common.file_io import run_file_io
from plugins.channels.contract import private_target_key
from plugins.ledger.contract import ContentPart, Input, MESSAGE_CATALOG, Message, MessageCatalog, Output, is_user_input


class SourcePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = False
    revision: int = Field(default=0, ge=0)
    excluded: tuple[str, ...] = ()
    preferred: tuple[str, ...] = ()
    fixed_session: str | None = None
    window_days: int = Field(default=7, ge=1, le=30)
    max_sessions: int = Field(default=3, ge=1, le=3)

    @model_validator(mode="after")
    def consent_version(self):
        if self.enabled and self.revision == 0:
            raise ValueError("自动来源需要独立、递增的授权 revision")
        return self

    def fingerprint(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class SourceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    session_id: str
    through_seq: int
    message_ids: tuple[str, ...]
    digest: str
    score: float


def text(message: Message) -> str:
    return "\n".join(part.value for part in message.body.parts
                     if isinstance(part, ContentPart) and part.kind == "text"
                     and isinstance(part.value, str)) if isinstance(message.body, (Input, Output)) else ""


def eligible(message: Message) -> bool:
    return message.source == "conversation" and (
        is_user_input(message) or message.author == "assistant"
        and isinstance(message.body, Output) and message.body.finish == "complete"
    )


def digest(messages: tuple[Message, ...]) -> str:
    return hashlib.sha256(json.dumps([
        [m.message_id, m.seq, m.recorded_at.isoformat(), m.author, text(m)] for m in messages
    ], ensure_ascii=False).encode()).hexdigest()


@dataclass
class Activity:
    head: int = -1
    # Derived references only: reset on generation replacement or prefix mutation.
    rows: list[Message] = field(default_factory=list)


class ActiveSources:
    def __init__(self) -> None:
        self.entries: dict[str, Activity] = {}
        self.revision: int | None = None

    def refresh(self, catalog: MessageCatalog, session_id: str, head: int, now: datetime, policy: SourcePolicy) -> Activity:
        revision = catalog.prefix_revision()
        if revision != self.revision:
            self.entries.clear()
            self.revision = revision
        if revision is None:
            self.entries.pop(session_id, None)
        activity = self.entries.setdefault(session_id, Activity())
        if head < activity.head:
            activity = self.entries[session_id] = Activity()
        reader = catalog.reader(session_id)
        cursor = activity.head
        additions: list[Message] = []
        while cursor < head:
            page = reader.read(after_seq=cursor, through_seq=head, source="conversation", limit=1000)
            if not page:
                break
            additions.extend(m for m in page if eligible(m) and text(m).strip())
            cursor = page[-1].seq
        cutoff = now - timedelta(days=policy.window_days)
        # Commit the cursor only after a complete successful scan; retries cannot double count.
        activity.rows = [m for m in (*activity.rows, *additions) if m.recorded_at >= cutoff]
        activity.head = head
        return activity

    async def capture(self, ctx: Context, policy: SourcePolicy, target: DeliveryTarget, now: datetime):
        catalog = ctx.require(MESSAGE_CATALOG)
        if target.channel != "akashic":
            raise PermissionError("active_sources_requires_native_private_web")
        with ctx.borrow(private_target_key(target.channel)) as proof:
            if proof is None:
                raise RuntimeError("private_provider_unavailable")
            target_guard = proof.guard(target.recipient, target.session_id)
        async with target_guard as private:
            if not private:
                raise PermissionError("target_unavailable")
            scope = catalog.attributes(target.session_id).scope
            heads = await catalog.snapshot_heads_async(prefix="akashic:", visibility="listed")
            self.entries = {k: v for k, v in self.entries.items() if k in heads and k not in policy.excluded}
            candidates = []
            rejected = []
            revision = catalog.prefix_revision()
            for session_id, head in heads.items():
                attrs = catalog.attributes(session_id)
                reason = ("excluded" if session_id in policy.excluded else
                          "learning_excluded" if attrs.learning != "eligible" else
                          "different_scope" if attrs.scope != scope else "")
                if reason:
                    rejected.append({"session_id": session_id, "reason": reason})
                    continue
                with ctx.borrow(private_target_key(target.channel)) as provider:
                    if provider is None:
                        raise RuntimeError("private_provider_unavailable")
                    guard = provider.guard(session_id.removeprefix("akashic:"), session_id)
                async with guard as allowed:
                    if not allowed:
                        rejected.append({"session_id": session_id, "reason": "private_identity_unproven"})
                        continue
                    activity = await run_file_io(lambda: self.refresh(catalog, session_id, head, now, policy))
                rows = tuple(m for m in activity.rows if m.recorded_at <= now)
                users = [m for m in rows if is_user_input(m)]
                if not users:
                    continue
                days = defaultdict(list)
                for m in users:
                    days[m.recorded_at.date()].append(m.recorded_at)
                score = sum((1 + min(len(stamps), 3) / 3) *
                            2 ** (-(now - max(stamps)).total_seconds() / 172800)
                            for stamps in days.values())
                candidates.append((session_id in policy.preferred, score, max(m.recorded_at for m in users), session_id, rows))
            if catalog.prefix_revision() != revision:
                self.entries.clear()
                raise RuntimeError("active_sources_prefix_changed_during_capture")
            candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]), reverse=True)
            # The fixed conversation is additional, not a priority boost within the quota.
            # Keep the most active source first for scoped memory queries.
            fixed = [item for item in candidates if item[3] == policy.fixed_session]
            ranked = [item for item in candidates if item[3] != policy.fixed_session]
            chosen = ranked[:policy.max_sessions] + fixed
            evidence, passive, queries = [], [], {}
            # Character budgets cover each serialized row, not just its text.
            budget = 6000
            for _, score, _, session_id, rows in chosen:
                last_user = max(i for i, m in enumerate(rows) if is_user_input(m))
                # Assistant/background tails must not push real user evidence out.
                selected = rows[max(0, last_user - 8):last_user + 3]
                remaining, rendered, used = min(2000, 6000 // len(chosen), budget), [], []
                # Reserve room for the user before surrounding assistant messages.
                ordered = sorted(selected, key=lambda m: (is_user_input(m), m.seq), reverse=True)
                for m in ordered:
                    row = {"session_id": session_id, "message_id": m.message_id,
                           "role": "user" if is_user_input(m) else "assistant",
                           "time": m.recorded_at.isoformat(), "text": "", "truncated": True}
                    room = min(remaining - len(json.dumps(row, ensure_ascii=False)) - 2, 800)
                    if room <= 0:
                        break
                    original = text(m)
                    row["text"] = original[:room]
                    row["truncated"] = len(original) > room
                    # JSON escaping may expand quotes/control characters.
                    while len(json.dumps(row, ensure_ascii=False)) > remaining:
                        room //= 2
                        row["text"], row["truncated"] = original[:room], True
                    cost = len(json.dumps(row, ensure_ascii=False))
                    rendered.append(row)
                    used.append(m)
                    remaining -= cost
                    budget -= cost
                # A conversational snippet without user evidence must not seed memory or chat.
                if not any(is_user_input(m) for m in used):
                    continue
                paired = sorted(zip(used, rendered), key=lambda pair: pair[0].seq)
                passive.extend(row for _, row in paired)
                evidence.append(SourceEvidence(session_id=session_id, through_seq=self.entries[session_id].head,
                    message_ids=tuple(m.message_id for m in used), digest=digest(tuple(used)), score=score))
                queries[session_id] = "\n".join(text(m) for m in used if is_user_input(m))[:800]
            return tuple(evidence), passive, queries, rejected[:20]


@asynccontextmanager
async def source_guard(ctx: Context, request: Request) -> AsyncIterator[str | None]:
    """Revalidate the frozen source consent at model admission, publication and first send."""
    if not request.source_policy:
        yield None
        return
    from .api import Config
    config = Config.model_validate(ctx.config)
    if (config.enabled is not True or not config.active_sources.enabled
            or config.active_sources.fingerprint() != request.source_policy
            or config.delivery != request.target or request.target.channel != "akashic"):
        yield "source_unavailable"
        return
    catalog = ctx.require(MESSAGE_CATALOG)
    async with AsyncExitStack() as stack:
        for sid in (request.target.session_id, *(e.session_id for e in request.source_evidence)):
            if not catalog.exists(sid) or not sid.startswith("akashic:"):
                yield "source_unavailable"
                return
            with ctx.borrow(private_target_key("akashic")) as provider:
                if provider is None:
                    yield "private_provider_unavailable"
                    return
                guard = provider.guard(sid.removeprefix("akashic:"), sid)
            if not await stack.enter_async_context(guard):
                yield "source_unavailable"
                return
        scope = catalog.attributes(request.target.session_id).scope
        def valid():
            for evidence in request.source_evidence:
                reader = catalog.reader(evidence.session_id)
                with reader.read_snapshot():
                    if (reader.deleted or reader.attributes.visibility != "listed"
                            or reader.attributes.learning != "eligible" or reader.attributes.scope != scope
                            or evidence.session_id in config.active_sources.excluded):
                        return False
                    rows = tuple(reader.get(mid) for mid in evidence.message_ids)
                    if any(m is None or not eligible(m) or m.seq > evidence.through_seq or m.recorded_at > request.now for m in rows):
                        return False
                    if digest(tuple(m for m in rows if m is not None)) != evidence.digest:
                        return False
            return bool(request.source_evidence)
        valid_sources = await run_file_io(valid)
        current_revision = ctx.require(PLUGIN_CONFIG).read(ctx).get("config_revision")
        yield None if valid_sources and current_revision == request.source_config_revision else "source_unavailable"
