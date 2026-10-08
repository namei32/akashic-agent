"""Hindsight wire/owner contracts with real message storage and controlled HTTP.

These are integration/regression tests, not LLM quality or companion benchmarks.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from agent.plugin_composition import (
    CompositionRoot,
    PluginRuntime,
)
from agent.plugin_composition.bindings import BINDINGS
from agent.plugin_composition.messages import MESSAGE_CATALOG, MESSAGE_EMBEDDINGS
from agent.plugin_composition.models import EMBEDDINGS, EmbeddingResult
from agent.plugin_contracts import CallRef, ContentPart, Input, Output, ToolCall
from agent.plugin_contracts.content import CONTENT
from agent.plugin_contracts.context import MATERIALS_V4
from agent.plugin_contracts.proactive import SEMANTIC_INTEREST
from agent.plugin_contracts.tools import TOOLS, CallSource
from agent.plugin_contracts.turns import TURN_PROJECTION
from plugins.content.api import legacy_post_commit_effect
from plugins.content.plugin import check_text
from plugins.context.materials import ContextMaterials
from plugins.hindsight.client import HindsightClient, HindsightError
from plugins.hindsight.config import Config, digest
from plugins.hindsight.interest import Interest
from plugins.hindsight.runtime import RecallTool, Runtime, check_recall
from plugins.hindsight.sources import (
    FEEDBACK_FIELDS,
)
from plugins.hindsight.store import Journal
from plugins.turn_projection.plugin import TurnProjection
from session.embedding_store import MessageEmbeddings
from session.log import MessageLog, SessionAttributes


class Backend:
    """Record actual HTTP requests; async ingestion can finish or lose its ack."""

    def __init__(self):
        self.operations, self.documents = {}, {}
        self.posts, self.recalls, self.retries = [], [], []
        self.auto_complete = True
        self.lose_ack = False
        self.response = None
        self.unavailable = False
        self.entered = self.release = None

    async def handle(self, request):
        if self.unavailable:
            return httpx.Response(503, json={"detail": "private-token-private-prompt"})
        parts = request.url.path.split("/")
        bank = parts[4]
        if request.method == "POST" and parts[-1] == "memories":
            body = json.loads(request.content)
            op = body["operation_id"]
            self.posts.append(body)
            self.operations.setdefault(
                op,
                {
                    "status": "completed" if self.auto_complete else "pending",
                    "bank": bank,
                    "item": body["items"][0],
                },
            )
            if self.auto_complete:
                self.complete(op)
            if self.lose_ack:
                self.lose_ack = False
                raise httpx.ReadTimeout("private-token-private-prompt")
            return httpx.Response(202, json={"operation_id": op, "async": True})
        if "operations" in parts and request.method == "GET":
            if self.entered is not None:
                self.entered.set()
                await self.release.wait()
            op = parts[-1]
            return (
                httpx.Response(404)
                if op not in self.operations
                else httpx.Response(200, json={"status": self.operations[op]["status"]})
            )
        if parts[-1] == "retry":
            op = parts[-2]
            self.retries.append(op)
            self.complete(op)
            return httpx.Response(200, json={"status": "pending"})
        if parts[-1] == "recall":
            self.recalls.append(json.loads(request.content))
            if self.response is not None:
                return httpx.Response(200, json=self.response)
            docs = [
                (doc, item)
                for (target, doc), item in self.documents.items()
                if target == bank
            ]
            docs.sort(key=lambda pair: pair[1]["timestamp"], reverse=True)
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "fact:" + doc,
                            "type": "world",
                            "document_id": doc,
                            "text": item["content"],
                        }
                        for doc, item in docs
                    ],
                    "source_facts": {},
                },
            )
        raise AssertionError(
            "unexpected request " + request.method + " " + request.url.path
        )

    def complete(self, op):
        row = self.operations[op]
        row["status"] = "completed"
        self.documents[(row["bank"], row["item"]["document_id"])] = row["item"]


class History:
    def __init__(self, records=()):
        self.records = list(records)
        self.calls = []

    async def pull(self, cursor, count):
        self.calls.append(cursor)
        return SimpleNamespace(
            after_cursor=cursor,
            records=[r for r in self.records if r.cursor > cursor][:count],
        )


def append_pair(
    log,
    number,
    user="我关注 AI 工程实践",
    answer="已记录你的要求",
    session="conversation",
):
    output = []
    for author, kind, body in (
        ("user", Input, Input((ContentPart("text", user),))),
        ("assistant", Output, Output((ContentPart("text", answer),), "complete")),
    ):
        output.append(
            log.writer(
                session,
                author=author,
                source="conversation",
                body_types=(kind,),
                content={"text": check_text},
            ).append(f"{session}:{author}:{number}", body)
        )
    return tuple(output)


def feedback(messages, cursor=1, kind="explicit_quote", confidence="gold"):
    values = {field: None for field in FEEDBACK_FIELDS}
    values.update(
        session_key=messages[0].session_id,
        user_message_id=messages[0].message_id,
        assistant_message_id=messages[-1].message_id,
        proactive_message_id="notification:1",
        feedback_type=kind,
        confidence=confidence,
        candidate_count=1,
        matched_by="reply_ref",
        reason="原回应关联到主动消息",
    )
    return SimpleNamespace(
        **values,
        cursor=cursor,
        event_id=f"proactive_feedback:{cursor}",
        payload_hash=digest(values),
    )


@asynccontextmanager
async def fixture(path, *, config=None, backend=None):
    cfg = config or Config(enabled=True)
    log = MessageLog(path / "sessions.db")
    log.ensure_session("conversation", SessionAttributes())
    remote = backend or Backend()
    http = httpx.AsyncClient(transport=httpx.MockTransport(remote.handle))
    client = HindsightClient(cfg, http=http, api_key="private-token")
    journal = Journal(path / "hindsight.sqlite3", cfg.storage_identity())
    runtime = Runtime(
        cfg, journal, client, log.catalog(), TurnProjection(), legacy_post_commit_effect
    )
    await runtime.initialize()
    try:
        yield runtime, log, remote
    finally:
        await runtime.close()
        await http.aclose()
        log.close()


def request(runtime, messages, query="用户偏好是什么", *, bank=None, limit=10):
    return {
        "query": query,
        "bank": bank
        or runtime.config.route(runtime.catalog.attributes(messages[0].session_id))[0],
        "timestamp": (messages[-1].recorded_at + timedelta(seconds=1)).isoformat(),
        "limit": limit,
        "max_chars": runtime.config.max_chars,
        "storage_identity": runtime.config.storage_identity(),
    }


@pytest.mark.asyncio
async def test_feedback_correction_reaches_next_wake_investigation_and_deduplicates(
    tmp_path,
):
    async with fixture(tmp_path) as (runtime, log, remote):
        first = append_pair(log, 1, "可以分享融资新闻")
        await runtime.tick()
        before = await runtime.query("before", request(runtime, first))
        assert "可以分享融资新闻" in before["reminders"][0]["text"]
        corrected = append_pair(log, 2, "以后少推融资新闻，我更想看工程实践")
        history = History([feedback(corrected)])
        runtime.history = history.pull
        original = log.reader("conversation").snapshot()
        await runtime.tick()
        after = await runtime.query("after", request(runtime, corrected))
        assert "以后少推融资新闻" in after["reminders"][0]["text"]
        assert "不是满意度或永久偏好的判定" in after["reminders"][0]["text"]
        assert {row["ref"] for row in after["references"]} >= {
            m.message_id for m in corrected
        }
        assert runtime.journal.status()["feedback_applied_cursor"] == 1
        count = len(remote.posts)
        await runtime.tick()
        assert len(remote.posts) == count
        assert history.calls == [0, 1]
        assert log.reader("conversation").snapshot() == original


@pytest.mark.asyncio
async def test_lost_submit_ack_is_probed_and_never_regenerated(tmp_path):
    async with fixture(tmp_path) as (runtime, log, remote):
        messages = append_pair(log, 1)
        remote.lose_ack = True
        await runtime.tick()
        assert runtime.journal.status()["jobs"] == {"queued": 1}
        await runtime.tick()
        assert len(remote.posts) == 1
        assert runtime.journal.status()["jobs"] == {"completed": 1}
        assert (
            "AI 工程实践"
            in (await runtime.query("q", request(runtime, messages)))["reminders"][0][
                "text"
            ]
        )


@pytest.mark.asyncio
async def test_pending_remote_job_resumes_after_host_restart(tmp_path):
    remote = Backend()
    remote.auto_complete = False
    async with fixture(tmp_path, backend=remote) as (runtime, log, _):
        append_pair(log, 1)
        await runtime.tick()
        op = remote.posts[0]["operation_id"]
        assert runtime.journal.status()["jobs"] == {"submitted": 1}
    remote.complete(op)
    async with fixture(tmp_path, backend=remote) as (runtime, _, _):
        await runtime.tick()
        assert len(remote.posts) == 1
        assert runtime.journal.status()["jobs"] == {"completed": 1}


@pytest.mark.asyncio
async def test_failed_remote_work_requires_explicit_retry_of_original_id(tmp_path):
    async with fixture(tmp_path) as (runtime, log, remote):
        append_pair(log, 1)
        remote.auto_complete = False
        await runtime.tick()
        op = remote.posts[0]["operation_id"]
        remote.operations[op]["status"] = "failed"
        await runtime.tick()
        job = "turn:" + digest(["conversation", "conversation:assistant:1"])
        assert runtime.journal.status()["jobs"] == {"failed": 1}
        await runtime.tick()
        assert remote.retries == []
        await runtime.retry(job)
        await runtime.tick()
        assert remote.retries == [op] and len(remote.posts) == 1
        assert runtime.journal.status()["jobs"] == {"completed": 1}


@pytest.mark.asyncio
async def test_disappeared_acknowledged_operation_is_not_resubmitted(tmp_path):
    async with fixture(tmp_path) as (runtime, log, remote):
        append_pair(log, 1)
        remote.auto_complete = False
        await runtime.tick()
        remote.operations.clear()
        await runtime.tick()
        assert len(remote.posts) == 1
        assert runtime.journal.status()["jobs"] == {"submitted": 1}


@pytest.mark.asyncio
async def test_close_drains_physical_request_and_lease_blocks_replacement(tmp_path):
    async with fixture(tmp_path) as (runtime, log, remote):
        append_pair(log, 1)
        remote.entered, remote.release = asyncio.Event(), asyncio.Event()
        tick = asyncio.create_task(runtime.tick())
        await asyncio.wait_for(remote.entered.wait(), 2)
        closing = asyncio.create_task(runtime.close())
        await asyncio.sleep(0)
        replacement = Runtime(
            runtime.config,
            Journal(runtime.journal.path, runtime.config.storage_identity()),
            runtime.client,
            log.catalog(),
            TurnProjection(),
            legacy_post_commit_effect,
        )
        with pytest.raises(BlockingIOError):
            await replacement.initialize()
        assert not closing.done()
        remote.release.set()
        await tick
        await closing
        await replacement.initialize()
        await replacement.close()


@pytest.mark.asyncio
async def test_scope_off_excluded_and_unfinished_turns_are_not_learned(tmp_path):
    cfg = Config(
        enabled=True,
        scope_policies={"project": {"off": "off", "a": "isolated", "b": "isolated"}},
    )
    async with fixture(tmp_path, config=cfg) as (runtime, log, remote):
        for name, attrs in (
            ("excluded", SessionAttributes(learning="excluded")),
            ("off", SessionAttributes.scoped({"project": "off"})),
            ("a", SessionAttributes.scoped({"project": "a"})),
            ("b", SessionAttributes.scoped({"project": "b"})),
        ):
            log.ensure_session(name, attrs)
            append_pair(log, 1, session=name, user="消息属于 " + name)
        log.writer(
            "conversation",
            author="user",
            source="conversation",
            body_types=(Input,),
            content={"text": check_text},
        ).append("unfinished", Input((ContentPart("text", "还没有回答"),)))
        await runtime.tick()
        assert len(remote.posts) == 2
        assert len({op["bank"] for op in remote.operations.values()}) == 2
        a = log.reader("a").snapshot()
        body = (await runtime.query("scope-a", request(runtime, a)))["reminders"][0][
            "text"
        ]
        assert "消息属于 a" in body and "消息属于 b" not in body
        assert cfg.route(log.catalog().attributes("off"))[1] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,confidence",
    [("unscored", "low"), ("topic_follow", "medium"), ("no_topic_follow", "low")],
)
async def test_weak_feedback_is_recorded_as_skipped_not_a_preference(
    tmp_path, kind, confidence
):
    async with fixture(tmp_path) as (runtime, log, remote):
        messages = append_pair(log, 1)
        runtime.history = History(
            [feedback(messages, kind=kind, confidence=confidence)]
        ).pull
        await runtime.tick()
        assert len(remote.posts) == 1
        assert runtime.journal.status()["feedback_discovered_cursor"] == 1
        assert runtime.journal.status()["feedback_applied_cursor"] == 1


@pytest.mark.asyncio
async def test_feedback_payload_drift_does_not_advance_cursor(tmp_path):
    async with fixture(tmp_path) as (runtime, log, _):
        row = feedback(append_pair(log, 1))
        row.reason = "changed after acceptance"
        runtime.history = History([row]).pull
        with pytest.raises(ValueError, match="payload_hash"):
            await runtime.tick()
        assert runtime.journal.feedback_cursor() == 0


def test_feedback_enqueue_and_cursor_are_one_transaction(tmp_path, monkeypatch):
    journal = Journal(tmp_path / "journal.db", "test")
    journal.initialize()
    original = Journal._enqueue

    def fail(db, key, bank, item):
        original(db, key, bank, item)
        raise OSError("controlled before cursor commit")

    monkeypatch.setattr(Journal, "_enqueue", staticmethod(fail))
    with pytest.raises(OSError):
        journal.record_feedback_page(
            0,
            [
                {
                    "cursor": 1,
                    "event_id": "proactive_feedback:1",
                    "payload_hash": "x",
                    "job_id": "j",
                    "bank": "b",
                    "item": {"document_id": "d", "content": "text"},
                }
            ],
        )
    assert journal.feedback_cursor() == 0 and journal.job("j") is None


@pytest.mark.asyncio
async def test_original_recall_receipt_is_reused_even_if_remote_memory_changes(
    tmp_path,
):
    async with fixture(tmp_path) as (runtime, log, remote):
        messages = append_pair(log, 1)
        await runtime.tick()
        args = request(runtime, messages)
        first = await runtime.query("same", args)
        remote.response = {"results": []}
        second = await runtime.query("same", args)
        assert first == second and len(remote.recalls) == 1
        with pytest.raises(ValueError, match="漂移"):
            await runtime.query("same", {**args, "query": "different"})


@pytest.mark.asyncio
async def test_future_original_sources_are_filtered_despite_backend_results(tmp_path):
    async with fixture(tmp_path) as (runtime, log, _):
        old = append_pair(log, 1, user="原先目标")
        append_pair(log, 2, user="后来改变目标")
        await runtime.tick()
        args = {**request(runtime, old), "timestamp": old[-1].recorded_at.isoformat()}
        body = (await runtime.query("past", args))["reminders"][0]["text"]
        assert "原先目标" in body and "后来改变目标" not in body


@pytest.mark.asyncio
async def test_fabricated_remote_provenance_fails_loud(tmp_path):
    async with fixture(tmp_path) as (runtime, log, remote):
        messages = append_pair(log, 1)
        await runtime.tick()
        remote.response = {
            "results": [
                {
                    "id": "fake",
                    "text": "用户喜欢一切",
                    "type": "world",
                    "document_id": "foreign",
                }
            ]
        }
        with pytest.raises(HindsightError, match="不属于"):
            await runtime.query("foreign", request(runtime, messages))


@pytest.mark.asyncio
async def test_observation_requires_complete_local_lineage(tmp_path):
    async with fixture(tmp_path) as (runtime, log, remote):
        messages = append_pair(log, 1)
        await runtime.tick()
        document = remote.posts[0]["items"][0]["document_id"]
        remote.response = {
            "results": [
                {
                    "id": "obs",
                    "text": "用户偏好工程实践",
                    "type": "observation",
                    "source_fact_ids": ["f"],
                }
            ],
            "source_facts": {
                "f": {"id": "f", "type": "world", "document_id": document}
            },
        }
        data = await runtime.query("obs", request(runtime, messages))
        assert (
            "用户偏好工程实践" in data["reminders"][0]["text"]
            and len(data["references"]) == 2
        )
        remote.response["source_facts"] = {}
        assert (await runtime.query("obs-truncated", request(runtime, messages)))[
            "references"
        ] == ()


@pytest.mark.asyncio
async def test_tool_requires_real_call_source_and_reuses_result(tmp_path):
    async with fixture(tmp_path) as (runtime, log, remote):
        append_pair(log, 1)
        await runtime.tick()
        log.save_binding("tool-binding", {"service": TOOLS.name, "version": 1})
        log.writer(
            "conversation",
            author="user",
            source="conversation",
            body_types=(Input,),
            content={"text": check_text},
        ).append("question", Input((ContentPart("text", "我的偏好呢"),)))
        calling = log.writer(
            "conversation",
            author="assistant",
            source="conversation",
            body_types=(Output,),
            content={"text": check_text},
            check_call=lambda _: None,
        ).append(
            "call", Output((ToolCall("tool-binding", {"query": "偏好"}),), "continue")
        )
        source = CallSource(
            CallRef(calling.message_id, 0), log.reader("conversation").snapshot()
        )
        tool = RecallTool(runtime)
        prepared = await tool.prepare({"query": "偏好"}, source)
        result = await tool.invoke(source.effect_key, prepared)
        assert (
            result.outcome == "success" and result.parts[1].kind == "hindsight.recall"
        )
        count = len(remote.recalls)
        assert await tool.query(source.effect_key) == result
        assert len(remote.recalls) == count
        with pytest.raises(ValueError, match="真实"):
            await tool.prepare(
                {"query": "偏好"}, replace(source, messages=source.messages[1:])
            )


@pytest.mark.asyncio
async def test_transport_errors_do_not_expose_token_or_prompt(tmp_path):
    async with fixture(tmp_path) as (runtime, log, remote):
        messages = append_pair(log, 1)
        remote.unavailable = True
        with pytest.raises(HindsightError) as caught:
            await runtime.query("error", request(runtime, messages))
        assert "503" in str(caught.value) and "private" not in str(caught.value)
        assert runtime.journal.recall_value("error") is None


def test_backend_or_scope_change_requires_explicit_migration(tmp_path):
    cfg = Config(enabled=True)
    path = tmp_path / "journal.db"
    Journal(path, cfg.storage_identity()).initialize()
    with pytest.raises(ValueError, match="不一致"):
        Journal(
            path, cfg.model_copy(update={"bank_prefix": "other"}).storage_identity()
        ).initialize()


class EmbeddingModel:
    descriptor = SimpleNamespace(identity="fixed", dimensions=3, model_id="fixed")

    def describe(self):
        return self.descriptor

    @asynccontextmanager
    async def bind(self, *, model_id=None):
        yield self

    async def embed(self, texts):
        return EmbeddingResult(
            tuple(
                (1.0, 0.0, 0.0) if "AI" in t or "工程" in t else (0.0, 1.0, 0.0)
                for t in texts
            )
        )


@pytest.mark.asyncio
async def test_interest_keeps_original_scale_fills_missing_cache_and_uses_cutoff(
    tmp_path,
):
    async with fixture(tmp_path) as (runtime, log, remote):
        messages = append_pair(log, 1, user="AI 工程实践", answer="AI 工程实践")
        embeddings = MessageEmbeddings(log)
        interest = Interest(
            runtime.config,
            log.catalog(),
            TurnProjection(),
            embeddings,
            EmbeddingModel(),
            legacy_post_commit_effect,
        )
        assert await interest.score(
            ["AI 工程文章", "体育比赛", ""], cutoff=messages[-1].recorded_at.isoformat()
        ) == (0.999, 0.0, 0.0)
        earlier = (messages[0].recorded_at - timedelta(seconds=1)).isoformat()
        assert await interest.score(["AI 工程文章"], cutoff=earlier) == (0.0,)
        assert remote.posts == [] and remote.recalls == []
        assert (
            embeddings.bind(
                lambda m: "\n".join(p.value for p in m.body.parts if p.kind == "text")
            ).read(messages[0], model="fixed", dimension=3)
            is not None
        )


@pytest.mark.asyncio
async def test_due_alert_still_bypasses_slow_interest_with_hindsight_contract(tmp_path):
    from datetime import UTC, datetime

    from plugins.eventmail.plugin import _StoreIO, _WakeServices
    from plugins.eventmail.store import EventMailStore
    from plugins.wake.admission import Duties
    from plugins.wake.state import WakeState

    entered, release = asyncio.Event(), asyncio.Event()

    class SlowModel(EmbeddingModel):
        async def embed(self, texts):
            entered.set()
            await release.wait()
            return await super().embed(texts)

    class Drift:
        async def snapshot(self, now):
            return {"proposals": ()}

    async with fixture(tmp_path) as (runtime, log, remote):
        append_pair(log, 1)
        remote.unavailable = True
        interest = Interest(
            runtime.config,
            log.catalog(),
            TurnProjection(),
            MessageEmbeddings(log),
            SlowModel(),
            legacy_post_commit_effect,
        )
        mail = EventMailStore(tmp_path / "mail.db")
        mail.initialize()
        state = WakeState(tmp_path / "wake.db")
        mail.submit(
            "feed",
            "batch",
            [{"item_id": "news", "revision": "r1", "payload": {"title": "AI 工程"}}],
        )
        duties = Duties(_WakeServices(mail, _StoreIO()), Drift(), state, interest)
        root = CompositionRoot("hindsight-alert-priority")
        now = datetime.now(UTC)

        async def maintain():
            async with root.context.runtime_scope():
                return await duties.maintain(now)

        job = asyncio.create_task(maintain())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            mail.report_alert(
                source_id="monitor",
                event_id="alert",
                payload={"message": "attention"},
                observed_at=now,
            )
            async with root.context.runtime_scope():
                admitted = await asyncio.wait_for(duties.check(now), 1)
            assert admitted.owner == "alert" and not job.done()
            assert mail.alert_status("monitor", "alert") == "pending"
            assert remote.posts == [] and remote.recalls == []
        finally:
            release.set()
            await job
            await root.dispose()


@pytest.mark.asyncio
async def test_interest_matches_existing_akasha_score_on_fixed_history(tmp_path):
    from plugins.akasha.interest import SemanticInterest as LegacyInterest
    from plugins.akasha.learning import Learning, LearningConfig

    async with fixture(tmp_path) as (runtime, log, _):
        pair = append_pair(log, 1, user="AI", answer="体育")
        vectors = MessageEmbeddings(log)
        records = vectors.bind(
            lambda m: "\n".join(p.value for p in m.body.parts if p.kind == "text")
        )
        records.save(pair[0], model="fixed", embedding=[1, 0, 0])
        records.save(pair[1], model="fixed", embedding=[0, 1, 0])
        model = EmbeddingModel()

        async def embed(texts):
            return [list(v) for v in (await model.embed(texts)).vectors]

        async def select():
            return LearningConfig(
                embedding_model="fixed", dimension=3, sources=runtime.config.sources
            ), embed

        learning = Learning(
            TurnProjection(),
            owner="fixture",
            post_commit_effect=legacy_post_commit_effect,
        )
        legacy = LegacyInterest(learning, log.catalog(), vectors, select)
        new = Interest(
            runtime.config,
            log.catalog(),
            TurnProjection(),
            vectors,
            model,
            legacy_post_commit_effect,
        )
        cutoff = pair[-1].recorded_at.isoformat()
        assert np.allclose(
            await new.score(["AI", "体育"], cutoff=cutoff),
            await legacy.score(["AI", "体育"], cutoff=cutoff),
            atol=1e-7,
        )


@pytest.mark.asyncio
async def test_real_plugin_activation_material_mask_and_cleanup(tmp_path, monkeypatch):
    from agent.plugin_contracts.tools import ToolRef
    from plugins.hindsight import plugin

    backend = Backend()
    http = httpx.AsyncClient(transport=httpx.MockTransport(backend.handle))
    client_class = HindsightClient
    monkeypatch.setattr(
        plugin, "HindsightClient", lambda cfg, **kwargs: client_class(cfg, http=http)
    )
    log = MessageLog(tmp_path / "sessions.db")
    log.ensure_session("conversation", SessionAttributes())
    append_pair(log, 1)

    class Content:
        legacy_post_commit_effect = staticmethod(legacy_post_commit_effect)

        async def register(self, ctx, definition):
            return await ctx.effect(lambda: lambda: None, label="test-content")

    class Tools:
        def __init__(self):
            self.items = {}

        async def declare_group(self, ctx, **kwargs):
            return await ctx.effect(lambda: lambda: None, label="test-tools-group")

        async def register(self, ctx, **kwargs):
            name = kwargs["name"]

            def setup():
                self.items[name] = kwargs
                return lambda: self.items.pop(name)

            await ctx.effect(setup, label="test-tool:" + name)
            return ToolRef(name, {"owner": ctx.runtime.plugin_id})

    root = CompositionRoot("hindsight-plugin")
    tools = Tools()
    materials = []

    async def services(ctx):
        current = ContextMaterials(ctx, prompt_sources={})
        materials.append(current)
        for key, value in (
            (MESSAGE_CATALOG, log.catalog()),
            (MESSAGE_EMBEDDINGS, MessageEmbeddings(log)),
            (TURN_PROJECTION, TurnProjection()),
            (CONTENT, Content()),
            (MATERIALS_V4, current),
            (TOOLS, tools),
            (BINDINGS, object()),
            (EMBEDDINGS, EmbeddingModel()),
        ):
            await ctx.provide(key, value)

    await root.mount(services, name="services")
    cfg = Config(enabled=True, poll_seconds=300)
    try:
        await root.mount(
            plugin.apply,
            name="hindsight",
            inject=plugin.inject,
            runtime=PluginRuntime(
                "hindsight",
                "test-generation",
                tmp_path,
                tmp_path / "plugin-data",
                tmp_path,
                cfg.model_dump(),
            ),
        )
        assert root.receipt().ready, root.receipt()
        async with tools.items["recall_memory"]["open"]({}) as tool:
            await tool.runtime.tick()
        assert len(backend.posts) == 1
        log.writer(
            "conversation",
            author="user",
            source="conversation",
            body_types=(Input,),
            content={"text": check_text},
        ).append("open-question", Input((ContentPart("text", "我的 AI 兴趣是什么"),)))
        snapshot = log.reader("conversation").snapshot()
        async with materials[0].bind(
            exclude_kinds=frozenset({"recall", "profile"})
        ) as view:
            hidden = await view.prepare(snapshot, "conversation")
        assert hidden["reminders"] == () and backend.recalls == []
        async with materials[0].bind() as view:
            shown = await view.prepare(snapshot, "conversation")
        assert "AI 工程实践" in shown["reminders"][0]["text"]
        assert root.context.require(SEMANTIC_INTEREST).status() is None
    finally:
        await root.dispose()
        assert tools.items == {}
        await http.aclose()
        log.close()


@pytest.mark.asyncio
async def test_controlled_wake_decision_changes_after_committed_user_correction(
    tmp_path,
):
    """Real PF→HTTP→recall tool→Wake decision receipt; only the model is a stub."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from agent.plugin_contracts import ToolResult
    from plugins.wake.api import DeliveryTarget
    from plugins.wake.messages import decision
    from plugins.wake.request import Phase, Request, check_phase, check_request
    from plugins.wake.tools import DecisionTool, Share, Skip

    async with fixture(tmp_path) as (runtime, log, backend):

        async def investigate():
            tools = {
                name: "test-binding:" + name
                for name in (
                    "screen_content",
                    "share_content",
                    "skip_content",
                    "recall_memory",
                )
            }
            for binding in (*tools.values(), "test-program", "test-sink"):
                log.save_binding(binding, {"version": 1, "service": TOOLS.name})
            request = Request(
                flow_id=uuid4().hex,
                owner="content",
                now=datetime.now(UTC),
                timezone="Asia/Shanghai",
                target=DeliveryTarget(
                    channel="test", recipient="user", session_id="conversation"
                ),
                sink={"name": "test", "address": "user", "binding_id": "test-sink"},
                program_binding="test-program",
                tools=tools,
                snapshot_seq=0,
                rules="",
                history="",
            )
            session = request.session_id
            log.ensure_session(
                session, SessionAttributes(visibility="internal", learning="excluded")
            )
            checks = {
                "wake.request": check_request,
                "wake.phase": check_phase,
                "text": check_text,
                "hindsight.recall": check_recall,
            }
            writer = log.writer(
                session,
                author="wake",
                source="wake",
                body_types=(Input, Output),
                content=checks,
                check_call=lambda _: None,
            )
            writer.append(
                request.input_id,
                Input((ContentPart("wake.request", request.model_dump(mode="json")),)),
            )
            writer.append(
                request.phase_id("investigate"),
                Input(
                    (
                        ContentPart(
                            "wake.phase",
                            Phase(
                                input_id=request.input_id, stage="investigate"
                            ).model_dump(),
                        ),
                    )
                ),
            )
            calling = writer.append(
                session + ":recall",
                Output(
                    (ToolCall(tools["recall_memory"], {"query": "融资新闻偏好"}),),
                    "continue",
                ),
            )
            source = CallSource(
                CallRef(calling.message_id, 0), log.reader(session).snapshot()
            )
            recall = RecallTool(runtime)
            prepared = await recall.prepare({"query": "融资新闻偏好"}, source)
            result = await recall.invoke(source.effect_key, prepared)
            assert result.outcome == "success"
            log.writer(
                session,
                author="tools",
                source="wake",
                body_types=(ToolResult,),
                content=checks,
                call_ref=source.call_ref,
            ).append(
                session + ":recall-result",
                ToolResult(source.call_ref, result.outcome, result.parts),
            )
            evidence = result.parts[0].value
            # A controlled reader uses the actual tool evidence, not the test's gold labels or journal.
            name = "skip_content" if "以后少推融资新闻" in evidence else "share_content"
            arguments = (
                {"reason": "用户明确要求少推融资新闻"}
                if name == "skip_content"
                else {"message": "融资新闻", "items": ["funding"]}
            )
            calling = writer.append(
                session + ":decision",
                Output((ToolCall(tools[name], arguments),), "continue"),
            )
            source = CallSource(
                CallRef(calling.message_id, 0), log.reader(session).snapshot()
            )
            bound = DecisionTool(name)
            accepted = await bound.prepare(arguments, source)
            assert isinstance(accepted, dict)
            outcome = await bound.invoke(source.effect_key, accepted)
            log.writer(
                session,
                author="tools",
                source="wake",
                body_types=(ToolResult,),
                content=checks,
                call_ref=source.call_ref,
            ).append(
                session + ":decision-result",
                ToolResult(source.call_ref, outcome.outcome, outcome.parts),
            )
            writer.append(
                session + ":end", Output((ContentPart("text", "done"),), "complete")
            )
            return decision(log.reader(session), request, "investigate")

        append_pair(log, 1, user="可以分享融资新闻")
        await runtime.tick()
        assert isinstance(await investigate(), Share)
        corrected = append_pair(log, 2, user="以后少推融资新闻，我更想看工程实践")
        runtime.history = History([feedback(corrected)]).pull
        await runtime.tick()
        assert isinstance(await investigate(), Skip)
        assert len(backend.posts) == 3  # internal Wake turns were not learned


@pytest.mark.asyncio
async def test_missing_feedback_message_does_not_silently_skip_history(tmp_path):
    async with fixture(tmp_path) as (runtime, log, _):
        row = feedback(append_pair(log, 1))
        row.user_message_id = "missing-original"
        row.payload_hash = digest(
            {field: getattr(row, field) for field in FEEDBACK_FIELDS}
        )
        runtime.history = History([row]).pull
        with pytest.raises(ValueError, match="缺失"):
            await runtime.tick()
        assert runtime.journal.feedback_cursor() == 0


@pytest.mark.asyncio
async def test_recall_include_uses_actual_hindsight_options_object(tmp_path):
    async with fixture(tmp_path) as (runtime, log, backend):
        pair = append_pair(log, 1)
        await runtime.tick()
        await runtime.query("wire-schema", request(runtime, pair))
        assert backend.recalls[0]["include"]["source_facts"] == {"max_tokens": 4096}


def test_plugin_is_importable_as_an_independent_package(tmp_path):
    import importlib.util
    import shutil
    import sys
    from pathlib import Path

    from agent.plugins.static_manifest import load_static_plugin_manifest

    package = tmp_path / "standalone"
    shutil.copytree(
        Path(__file__).resolve().parents[1] / "plugins" / "hindsight",
        package,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    manifest = load_static_plugin_manifest(package)
    assert (manifest.name, manifest.version, manifest.api_version) == (
        "hindsight",
        "1.0.0",
        3,
    )
    name = "_standalone_hindsight_contract"
    spec = importlib.util.spec_from_file_location(
        name, package / "__init__.py", submodule_search_locations=[str(package)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        plugin = __import__(name + ".plugin", fromlist=["apply"])
        assert callable(plugin.apply) and plugin.Config().enabled is False
    finally:
        for key in list(sys.modules):
            if key == name or key.startswith(name + "."):
                del sys.modules[key]


@pytest.mark.asyncio
async def test_default_plugin_is_inert_and_does_not_create_data(tmp_path):
    from plugins.hindsight import plugin

    root = CompositionRoot("hindsight-disabled")
    try:
        await root.mount(
            plugin.apply,
            name="hindsight",
            inject=(),
            runtime=PluginRuntime(
                "hindsight", "disabled", tmp_path, tmp_path / "private", tmp_path, {}
            ),
        )
        assert root.receipt().ready
        assert not (tmp_path / "private").exists()
        assert root.context.get(SEMANTIC_INTEREST) is None
    finally:
        await root.dispose()
