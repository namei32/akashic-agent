"""Memory-initiative consumption without a hard dependency on any provider."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta
import json
from typing import Protocol, cast

from agent.plugin_composition import ServiceKey
from plugins.content.contract import Content
from plugins.delivery.contract import ScopedDeliveryHistory
from plugins.ledger.contract import ContentPart, Input, MessageCatalog, Output

from .messages import _preview

try:
    import plugins.default_memory.contract as _memory_contract
except ModuleNotFoundError as error:
    if error.name not in {"plugins.default_memory", "plugins.default_memory.contract"}:
        raise
    _memory_contract = None


class InitiativeMemory(Protocol):
    async def admit(self, request_id: str, session_id: str, query: str, *,
                    timestamp: str, limit: int = 4, max_chars: int = 1500) -> dict[str, object]: ...
    async def prepare(self, request_id: str, session_id: str, query: str, *,
                      timestamp: str, limit: int = 4, max_chars: int = 1500) -> dict[str, object]: ...
    def guard(self, request_id: str, session_id: str) -> AbstractAsyncContextManager[str | None]: ...


# Older or uninstalled providers leave the optional capability absent. Its
# identity is always declared by Default Memory's public contract, never here.
INITIATIVE_MEMORY = cast(ServiceKey[InitiativeMemory] | None,
                         getattr(_memory_contract, "INITIATIVE_MEMORY", None))


def capture(catalog: MessageCatalog, content: Content, history: ScopedDeliveryHistory, *,
            session_id: str, now: datetime) -> tuple[int | None, str, str, str]:
    """Freeze only the target Session's user evidence and confirmed deliveries."""
    if not catalog.exists(session_id):
        return None, "", "", "source_unavailable"
    reader = catalog.reader(session_id)
    with reader.read_snapshot():
        if reader.deleted or reader.attributes.visibility != "listed":
            return None, "", "", "source_unavailable"
        page = reader.read_tail(limit=200)
        passive: list[dict[str, object]] = []
        inputs: list[str] = []
        remaining = 800
        for message in reversed(page.messages):
            if (message.recorded_at > now or message.source not in {"conversation", "legacy-unattributed"}
                    or not isinstance(message.body, (Input, Output))):
                continue
            if isinstance(message.body, Input) and not content.is_user_input(message):
                continue
            text = "\n".join(cast(str, part.value) for part in message.body.parts
                             if isinstance(part, ContentPart) and part.kind == "text"
                             and isinstance(part.value, str)).strip()
            if not text:
                continue
            if isinstance(message.body, Input) and len(inputs) < 3 and remaining > 0:
                item = text[:remaining]
                inputs.append(item)
                remaining -= len(item) + 1
            if len(passive) < 20:
                passive.append({"message_id": message.message_id,
                                "role": "user" if isinstance(message.body, Input) else "assistant",
                                "time": message.recorded_at.isoformat(), "text": text})
    delivered = history.recent_for_session(
        session_id, since=now - timedelta(days=7), until=now, limit=30,
        excluded_sources=frozenset({"conversation"}),
    )
    proactive = [{"message_id": item.message.message_id, "session_id": item.message.session_id,
                  "confirmed_at": item.confirmed_at.isoformat(), "text": "\n".join(
                      cast(str, part.value) for part in item.message.body.parts
                      if isinstance(part, ContentPart) and part.kind == "text" and isinstance(part.value, str))}
                 for item in delivered if isinstance(item.message.body, Output)]
    result: Mapping[str, object] = {
        "recent_conversation": list(reversed(_preview(passive, 3000))),
        "delivered_messages": list(reversed(_preview(proactive, 3000))),
    }
    return page.through_seq, "\n".join(reversed(inputs)), json.dumps(result, ensure_ascii=False), ""
