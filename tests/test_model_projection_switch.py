"""Switch models without losing visible history or reusing another binding's state."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
from types import SimpleNamespace

import pytest

from agent.plugin_composition import LLMResponse, ModelContinuation, ToolCall as ModelToolCall
from agent.plugin_contracts import CallRef, ContentPart, ContentReferences, Input, Message, Output, ToolCall, ToolResult
from plugins.models.projection import MessageProjection, response_facts
from plugins.models.store import MODEL_CALLS_SCHEMA, ModelCallReader


@pytest.fixture
def history(tmp_path):
    database = tmp_path / "calls.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(MODEL_CALLS_SCHEMA)
        connection.executemany(
            "INSERT INTO model_calls(id,binding_json,request_digest,state) VALUES (?,?,?,?)",
            [(identity, json.dumps({"binding_id": "old-binding"}), "fixture", "success")
             for identity in ("old-call-1", "old-call-2")],
        )

    @contextmanager
    def connect():
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    continuation = ModelContinuation("old-binding", {"provider_state": "fixture-only"})
    tool_facts = response_facts(LLMResponse(
        content=None,
        tool_calls=[ModelToolCall("provider-call-1", "lookup", {"key": "example"})],
        call_record_id="old-call-1",
    ), (0,))
    answer_facts = response_facts(LLMResponse(
        content="Earlier answer", continuation=continuation, call_record_id="old-call-2",
    ), ())
    bodies = (
        Input((ContentPart("text", "Original question"),)),
        Output((ToolCall("lookup-binding", {"key": "example"}), tool_facts), "continue"),
        ToolResult(CallRef("message-1", 0), "success", (ContentPart("text", "Tool evidence"),)),
        Output((ContentPart("text", "Earlier answer"), answer_facts), "complete"),
        Input((ContentPart("text", "Follow-up question"),)),
    )
    messages = tuple(Message(
        message_id=f"message-{index}", session_id="chat:test", seq=index,
        recorded_at=datetime(2026, 10, 8, tzinfo=timezone.utc),
        author="user" if isinstance(body, Input) else "assistant",
        source="conversation", body=body,
    ) for index, body in enumerate(bodies))
    return database, ModelCallReader(connect), messages, continuation


def projection(reader, binding_id):
    return MessageProjection(
        SimpleNamespace(descriptor=SimpleNamespace(binding_id=binding_id)),
        source="conversation",
        render_content=lambda part: ({"type": "text", "text": part.value},),
        tool_name=lambda _: "lookup",
        read_call=reader,
        check_summary=lambda _: ContentReferences(),
    )


@pytest.mark.parametrize("binding_id", ["old-binding", "new-binding"])
def test_binding_switch_preserves_messages_and_tool_pairs(history, binding_id):
    database, reader, messages, continuation = history
    with sqlite3.connect(database) as connection:
        before = connection.execute("SELECT * FROM model_calls ORDER BY id").fetchall()

    request = projection(reader, binding_id).render(messages, after_seq=-1)

    assert [row["role"] for row in request.messages] == ["user", "assistant", "tool", "assistant", "user"]
    assert request.messages[0]["content"][0]["text"] == "Original question"
    assert request.messages[1]["tool_calls"][0]["id"] == "provider-call-1"
    assert request.messages[2]["tool_call_id"] == "provider-call-1"
    assert request.messages[2]["content"][0]["text"] == "Tool evidence"
    assert request.messages[3]["content"][0]["text"] == "Earlier answer"
    assert request.messages[4]["content"][0]["text"] == "Follow-up question"
    assert request.continuation == (continuation if binding_id == "old-binding" else None)
    assert messages[3].body.parts[1].value["continuation"]["binding_id"] == "old-binding"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM model_calls ORDER BY id").fetchall() == before


@pytest.mark.parametrize("damage", ["missing", "mismatched", "unsettled"])
def test_binding_switch_still_rejects_invalid_historical_receipts(history, damage):
    database, reader, messages, _ = history
    with sqlite3.connect(database) as connection:
        if damage == "missing":
            connection.execute("DELETE FROM model_calls WHERE id='old-call-2'")
        elif damage == "mismatched":
            connection.execute("UPDATE model_calls SET binding_json=? WHERE id='old-call-2'",
                               (json.dumps({"binding_id": "unrelated-binding"}),))
        else:
            connection.execute("UPDATE model_calls SET state='error' WHERE id='old-call-2'")
    expected_error = KeyError if damage == "missing" else ValueError
    with pytest.raises(expected_error):
        projection(reader, "new-binding").render(messages, after_seq=-1)
