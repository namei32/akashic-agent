from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Protocol

from agent.plugin_composition import ServiceKey
from agent.plugin_composition.messages import MessageCatalog
from agent.plugin_contracts import ContentPart, Input, Message, Output
from agent.plugin_contracts.turns import TurnProjection

from .config import Config, digest


class FeedbackRecord(Protocol):
    cursor: int
    event_id: str
    payload_hash: str
    session_key: str
    user_message_id: str
    assistant_message_id: str
    proactive_message_id: str | None
    feedback_type: str
    confidence: str
    pa_score: float | None
    pua_score: float | None
    lag_seconds: int | None
    candidate_count: int
    matched_by: str
    reason: str
    user_content_preview: str | None
    assistant_content_preview: str | None
    proactive_content_preview: str | None


class FeedbackPage(Protocol):
    after_cursor: int
    records: Sequence[FeedbackRecord]


class FeedbackHistory(Protocol):
    def page(self, *, after_cursor: int, max_items: int) -> FeedbackPage: ...


Suppress = Callable[[Message], str | None]


PROACTIVE_FEEDBACK_HISTORY = ServiceKey[FeedbackHistory](
    "proactive-feedback.history.v1"
)
FEEDBACK_FIELDS = (
    "session_key",
    "user_message_id",
    "assistant_message_id",
    "proactive_message_id",
    "feedback_type",
    "confidence",
    "pa_score",
    "pua_score",
    "lag_seconds",
    "candidate_count",
    "matched_by",
    "reason",
    "user_content_preview",
    "assistant_content_preview",
    "proactive_content_preview",
)


def text(message: Message) -> str:
    body = message.body
    if not isinstance(body, (Input, Output)):
        return ""
    return "\n".join(
        part.value
        for part in body.parts
        if isinstance(part, ContentPart)
        and part.kind == "text"
        and isinstance(part.value, str)
    )


def approved_turns(
    catalog: MessageCatalog,
    projection: TurnProjection,
    config: Config,
    suppress: Suppress,
    *,
    cutoff: datetime | None = None,
) -> tuple[tuple[str, tuple[Message, ...]], ...]:
    samples: list[tuple[str, tuple[Message, ...]]] = []
    for session, head in catalog.snapshot_heads().items():
        bank, learn = config.route(catalog.attributes(session))
        if not learn:
            continue
        messages = catalog.reader(session).snapshot(through_seq=head)
        by_id = {m.message_id: m for m in messages}
        for source in sorted({m.source for m in messages} & set(config.sources)):
            for turn in projection.project(messages, source):
                if turn.status != "complete" or turn.ending_message_id is None:
                    continue
                members = tuple(by_id[mid] for mid in turn.message_ids)
                ending = by_id[turn.ending_message_id]
                observations = tuple(by_id[mid] for _, mid in turn.observations)
                if (
                    not isinstance(ending.body, Output)
                    or ending.body.finish != "complete"
                    or any(suppress(m) == "suppress" for m in (*members, *observations))
                    or not any(
                        isinstance(m.body, Input) and text(m).strip() for m in members
                    )
                    or not text(ending).strip()
                    or (cutoff is not None and ending.recorded_at > cutoff)
                ):
                    continue
                samples.append((bank, members))
    return tuple(
        sorted(
            samples,
            key=lambda pair: (
                pair[1][-1].recorded_at,
                pair[1][-1].session_id,
                pair[1][-1].seq,
            ),
        )
    )


def source_manifest(messages: Sequence[Message]) -> dict[str, object]:
    return {
        "session_id": messages[0].session_id,
        "message_ids": [m.message_id for m in messages],
        "source_digest": digest(
            [
                [
                    m.message_id,
                    m.seq,
                    m.author,
                    m.source,
                    m.recorded_at.isoformat(),
                    text(m),
                ]
                for m in messages
            ]
        ),
    }


def transcript(messages: Sequence[Message]) -> str:
    return "\n\n".join(
        f"{m.author} ({m.recorded_at.isoformat()}), message_id={m.message_id}:\n{text(m)}"
        for m in messages
        if text(m).strip()
    )


def turn_item(messages: tuple[Message, ...]) -> tuple[str, dict[str, object]]:
    manifest = source_manifest(messages)
    identity = "turn:" + digest([manifest["session_id"], messages[-1].message_id])
    return identity, {
        "document_id": identity,
        "content": transcript(messages),
        "timestamp": messages[-1].recorded_at.isoformat(),
        "context": "实际已提交的用户与 Agent 对话。保留说话者、时间和明确纠正；助手推测不等于用户陈述。",
        "metadata": {
            key: json.dumps(value, ensure_ascii=False)
            if isinstance(value, list)
            else value
            for key, value in manifest.items()
        },
        "tags": ["akashic:conversation"],
    }


def feedback_rows(
    page: FeedbackPage,
    catalog: MessageCatalog,
    projection: TurnProjection,
    config: Config,
    suppress: Suppress,
) -> tuple[dict[str, object], ...]:
    """Only persist accepted, immutable PF history; matching is not sentiment."""
    result: list[dict[str, object]] = []
    if not page.records:
        return ()
    last = page.after_cursor
    approved = {
        tuple(m.message_id for m in messages): (bank, messages)
        for bank, messages in approved_turns(catalog, projection, config, suppress)
    }
    for record in page.records:
        payload = {field: getattr(record, field) for field in FEEDBACK_FIELDS}
        if (
            type(record.cursor) is not int
            or record.cursor <= last
            or record.event_id != f"proactive_feedback:{record.cursor}"
            or digest(payload) != record.payload_hash
        ):
            raise ValueError("反馈历史身份、顺序或 payload_hash 无效")
        last = record.cursor
        row: dict[str, object] = {
            "cursor": record.cursor,
            "event_id": record.event_id,
            "payload_hash": record.payload_hash,
            "job_id": None,
        }
        if record.feedback_type not in {
            "explicit_quote",
            "topic_follow",
            "no_topic_follow",
            "unscored",
        }:
            raise ValueError("反馈类型无效")
        if record.confidence not in {"gold", "high", "medium", "low"}:
            raise ValueError("反馈置信度无效")
        bank, learn = config.route(catalog.attributes(record.session_key))
        if learn:
            reader = catalog.reader(record.session_key)
            user, assistant = (
                reader.get(record.user_message_id),
                reader.get(record.assistant_message_id),
            )
            if user is None or assistant is None:
                raise ValueError("反馈引用的原始 Message 缺失，不能推进消费 cursor")
            if not isinstance(user.body, Input) or not isinstance(
                assistant.body, Output
            ):
                raise ValueError("反馈原始 Message 类型与身份不一致")
        chosen = next(
            (
                (bank, messages)
                for bank, messages in approved.values()
                if messages[-1].session_id == record.session_key
                and messages[-1].message_id == record.assistant_message_id
                and any(
                    m.message_id == record.user_message_id and isinstance(m.body, Input)
                    for m in messages
                )
            ),
            None,
        )
        if chosen is None:
            # Excluded/open/abandoned turns cannot be smuggled in via feedback.
            row["skip_reason"] = "没有符合学习准入的已完成原对话"
        elif record.feedback_type == "unscored" or record.confidence not in {
            "gold",
            "high",
        }:
            row["skip_reason"] = "未评分或关联证据置信度不足"
        else:
            bank, messages = chosen
            manifest = source_manifest(messages)
            identity = "feedback:" + record.event_id
            item = {
                "document_id": identity,
                "content": (
                    "主动互动关联记录（不是满意度或永久偏好的判定）。\n"
                    f"关联类型={record.feedback_type}，置信度={record.confidence}，"
                    f"主动消息ID={record.proactive_message_id or '未定位'}。\n"
                    "以下是原始已提交对话；只提取用户明确表达的要求及适用时间，不把引用/接续视作喜欢。\n"
                    + transcript(messages)
                ),
                "timestamp": messages[-1].recorded_at.isoformat(),
                "context": "用户回应主动消息的有出处记录。匹配置信度只表示关联，不表示正面情感。",
                "metadata": {
                    **{
                        k: json.dumps(v, ensure_ascii=False)
                        if isinstance(v, list)
                        else v
                        for k, v in manifest.items()
                    },
                    "feedback_event_id": record.event_id,
                    "feedback_payload_hash": record.payload_hash,
                    "feedback_type": record.feedback_type,
                    "confidence": record.confidence,
                },
                "tags": ["akashic:feedback"],
            }
            row.update(bank=bank, job_id=identity, item=item)
        result.append(row)
    return tuple(result)


def original_messages(
    item: Mapping[str, object], catalog: MessageCatalog
) -> tuple[Message, ...]:
    metadata = item.get("metadata")
    if not isinstance(metadata, Mapping) or not isinstance(
        metadata.get("session_id"), str
    ):
        raise TypeError("已提交材料的出处格式无效")
    ids = json.loads(str(metadata.get("message_ids")))
    if (
        not isinstance(ids, list)
        or not ids
        or any(not isinstance(mid, str) for mid in ids)
    ):
        raise ValueError("材料原 Message 身份无效")
    reader = catalog.reader(metadata["session_id"])
    messages = tuple(reader.get(mid) for mid in ids)
    if any(m is None for m in messages):
        raise ValueError("原始 Message 缺失，不能伪造召回出处")
    typed = tuple(m for m in messages if m is not None)
    if source_manifest(typed)["source_digest"] != metadata.get("source_digest"):
        raise ValueError("召回引用的原始材料发生漂移")
    return typed
