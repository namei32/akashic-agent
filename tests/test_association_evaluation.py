from contextlib import closing
from datetime import UTC, datetime, timedelta
import json

import numpy as np
import pytest

from eval.association.datasets import locomo, longmemeval, prepare
from eval.association.embeddings import VectorCache, populate
from eval.association.retrieval import Full, Hybrid, cycle_fingerprint, evidence_metrics, materialize, render
from eval.association.runner import jsonl, run_answers, run_retrieval
from eval.association.runner import select_cases
from eval.association.schema import Case, Corpus, Evidence, Settings, Suite, Unit, digest
from eval.association.schema import file_digest
from eval.association.report import build_report, paired_interval
from eval.association.scoring import OfficialScorers, run_judgments


def sample():
    stamp = datetime(2020, 1, 1, tzinfo=UTC)
    units = tuple(Unit(id=f"u{i}", messages=(Evidence(
        id=f"m{i}", session_id=f"s{i}", speaker="Caroline", role="observation",
        at=stamp + timedelta(days=i), text=text,
    ),)) for i, text in enumerate(("red kite library", "blue ocean workshop", "green pottery class")))
    corpus = Corpus(id="history", adapter="fixture-observations-v1", units=units)
    cases = (
        Case(id="q1", dataset="locomo", corpus_id="history", cluster_id="c1", split="dev",
             category="multi-hop", official_category=1, question="red library", cutoff=stamp + timedelta(days=4),
             answer="SECRET-GOLD-DO-NOT-SEND", gold_ids=("m0",), gold_level="message"),
        Case(id="q2", dataset="locomo", corpus_id="history", cluster_id="c1", split="dev",
             category="adversarial", official_category=5, question="unknown building",
             cutoff=stamp + timedelta(days=4), answer="", unanswerable=True),
    )
    return Suite(sources={"fixture": "true"}, corpora=(corpus,), cases=cases)


def cache_for(tmp_path, suite):
    cache = VectorCache(tmp_path / "vectors.sqlite3", model="fixture", dimensions=2, space="fixture-v1", writable=True)
    for i, unit in enumerate(suite.corpora[0].units):
        cache.put(unit.input_text, ((1., 0.), (.4, .9), (0., 1.))[i])
    for case in suite.cases:
        cache.put(case.question, [1., 0.])
    return cache


def settings():
    return Settings(model="fixture-reader", embedding_model="fixture", dimensions=2,
                    budget_chars=300, repeats=2)


def test_hybrid_uses_both_rankings_without_constructing_graph(tmp_path, monkeypatch):
    suite = sample()
    with closing(cache_for(tmp_path, suite)) as cache:
        turns, _ = materialize(suite.corpora[0], cache)
        def forbidden(*args, **kwargs):
            raise AssertionError("Hybrid must not build a graph")
        monkeypatch.setattr("eval.association.retrieval.MemoryCycle", forbidden)
        candidates, detail = Hybrid(turns, settings()).query(suite.cases[0], cache.get("red library"))
        assert candidates[0]["unit_id"] == "u0"
        assert candidates[0]["ranks"] == {"dense": 1, "bm25": 1}
        assert candidates[0]["score"] == pytest.approx(2 / 61)
        assert not detail["graph_built"]
        _, no_lexical = Hybrid(turns, settings()).query(suite.cases[1], cache.get("unknown building"))
        assert no_lexical["bm25_candidates"] == 0


def test_full_calls_production_memory_without_mutating_published_state(tmp_path):
    suite = sample()
    with closing(cache_for(tmp_path, suite)) as cache:
        turns, state = materialize(suite.corpora[0], cache)
        full = Full(turns, state, settings())
        before = cycle_fingerprint(full.cycle)
        first, detail = full.query(suite.cases[0], cache.get("red library"))
        full.query(suite.cases[1], cache.get("unknown building"))
        second, _ = full.query(suite.cases[0], cache.get("red library"))
        assert first == second
        assert detail["read_only_verified"] and detail["graph_version"] == 3
        assert cycle_fingerprint(full.cycle) == before
        assert all(hit["unit_id"] in {"u0", "u1", "u2"} for hit in first)


def test_render_measures_presented_evidence_not_candidate_or_hub_count():
    suite = sample()
    corpus, case = suite.corpora[0], suite.cases[0]
    candidates = [{"unit_id": "u1"}, {"unit_id": "u0"}, {"unit_id": "u0"}]
    full = render(corpus, candidates, 1000)
    budget = len(full["context"].split("[id=m0")[0])
    material = render(corpus, candidates, budget)
    assert material["presented_ids"] == ["m1"]
    assert material["candidate_evidence_ids"] == ["m1", "m0"]
    score = evidence_metrics(case, corpus, material["presented_ids"], material["candidate_evidence_ids"])
    assert score["recall"] == 0 and score["recall_at_5"] == 1
    assert material["context_chars"] <= budget
    assert evidence_metrics(suite.cases[1], corpus, [], [])["recall"] is None


def test_vector_cache_rejects_wrong_space_shape_and_overwrite(tmp_path):
    path = tmp_path / "vectors.db"
    with closing(VectorCache(path, model="m", dimensions=2, space="one", writable=True)) as cache:
        cache.put("hello", [3, 4])
        np.testing.assert_allclose(cache.get("hello"), [.6, .8])
        before = cache.fingerprint()
        with pytest.raises(ValueError, match="overwrite"):
            cache.put("hello", [1, 0])
        with pytest.raises(ValueError):
            cache.put("zero", [0, 0])
        with pytest.raises(KeyError):
            cache.get("not embedded")
        assert before == cache.fingerprint()
    with pytest.raises(ValueError, match="space"):
        VectorCache(path, model="m", dimensions=2, space="two")
    with pytest.raises(ValueError, match="dimensions"):
        VectorCache(path, model="m", dimensions=3)


def test_longmemeval_preserves_labels_and_prevents_future_evidence(tmp_path):
    row = {"question_id": "id", "question_type": "multi-session", "question": "where?",
           "answer": "SECRET", "question_date": "2020-01-02", "haystack_session_ids": ["s1", "s2"],
           "haystack_dates": ["2020-01-01", "2020-01-03"], "answer_session_ids": ["s1"],
           "haystack_sessions": [[{"role": "user", "content": "The workshop is in York", "has_answer": True},
                                  {"role": "assistant", "content": "Understood"}],
                                 [{"role": "user", "content": "Future knowledge"}]]}
    path = tmp_path / "lme.json"
    path.write_text(json.dumps([row]))
    suite = prepare(lme_dev=path)
    assert "SECRET" not in suite.corpora[0].model_dump_json()
    assert len(suite.corpora[0].visible(suite.cases[0].cutoff).units) == 1
    row["haystack_sessions"][1][0]["has_answer"] = True
    path.write_text(json.dumps([row]))
    strict = prepare(lme_dev=path)
    assert strict.cases[0].data_issues == ("gold_after_question_time",)
    assert len(strict.corpora[0].visible(strict.cases[0].cutoff).units) == 1
    provided = prepare(lme_dev=path, time_policy="provided-history")
    assert not provided.cases[0].data_issues
    assert provided.cases[0].question_at == strict.cases[0].question_at
    assert provided.cases[0].cutoff > strict.cases[0].cutoff


def test_repeated_lme_source_session_ids_keep_distinct_occurrences(tmp_path):
    row = {"question_id": "id", "question_type": "multi-session", "question": "where?",
           "answer": "York", "question_date": "2020-01-03", "haystack_session_ids": ["same", "same"],
           "haystack_dates": ["2020-01-01", "2020-01-02"], "answer_session_ids": ["same"],
           "haystack_sessions": [[{"role": "user", "content": "First history"}],
                                 [{"role": "user", "content": "Second history"}]]}
    path = tmp_path / "duplicate-source-id.json"
    path.write_text(json.dumps([row]))
    suite = prepare(lme_dev=path)
    corpus, case = suite.corpora[0], suite.cases[0]
    messages = [m for u in corpus.units for m in u.messages]
    assert len({m.id for m in messages}) == 2 and len({m.session_id for m in messages}) == 2
    assert len({m.source_session_id for m in messages}) == 1
    assert evidence_metrics(case, corpus, [messages[1].id], [messages[1].id])["recall"] == 1


def test_empty_history_input_is_preserved_but_not_embedded_or_rendered(tmp_path):
    suite = sample()
    empty = suite.corpora[0].units[0].messages[0].model_copy(update={"id": "empty", "text": ""})
    unit = Unit(id="empty", messages=(empty,))
    corpus = suite.corpora[0].model_copy(update={"units": (unit, *suite.corpora[0].units)})
    with closing(cache_for(tmp_path, suite)) as cache:
        turns, state = materialize(corpus, cache)
        assert len(turns) == 3
        state.check_turns(turns)
    assert render(corpus, [{"unit_id": "empty"}], 300)["presented_ids"] == []


def test_locomo_split_keeps_conversations_and_original_speakers(tmp_path):
    source = []
    for i in range(5):
        source.append({"sample_id": str(i), "conversation": {
            "session_1_date_time": "1:56 pm on 8 May, 2023",
            "session_1": [{"speaker": "Caroline", "dia_id": "D1:1", "text": "My pottery studio is in York"},
                          {"speaker": "Melanie", "dia_id": "D1:2", "text": "I prefer the Leeds studio"}]},
            "observation": "DO NOT IMPORT", "qa": [
                {"category": 1, "question": "Where does Caroline go?", "answer": "York", "evidence": ["D1:1"]},
                {"category": 5, "question": "Which building?", "adversarial_answer": "WRONG-ANSWER"},
            ]})
    path = tmp_path / "locomo.json"
    path.write_text(json.dumps(source))
    corpora, cases = locomo(path)
    assert cases == locomo(path)[1]
    dev = {q.cluster_id for q in cases if q.split == "dev"}
    test = {q.cluster_id for q in cases if q.split == "holdout"}
    assert len(dev) == 2 and not dev & test
    assert {m.speaker for c in corpora for u in c.units for m in u.messages} == {"Caroline", "Melanie"}
    assert all(m.role == "observation" for c in corpora for u in c.units for m in u.messages)
    assert all("WRONG-ANSWER" not in c.model_dump_json() and "DO NOT IMPORT" not in c.model_dump_json() for c in corpora)


class Reader:
    def __init__(self):
        self.requests = []

    async def complete(self, system, user, output_tokens):
        self.requests.append((system, user, output_tokens))
        assert "SECRET-GOLD" not in user and "SECRET-GOLD" not in system
        return {"model": "fixture-reader", "effort": "max", "text": "A cited answer [m0]",
                "usage": {"input_tokens": 20, "output_tokens": 6}}


@pytest.mark.asyncio
async def test_paired_pipeline_keeps_every_attempt_and_hides_gold(tmp_path):
    suite, config = sample(), settings()
    with closing(cache_for(tmp_path, suite)) as cache:
        run_retrieval(suite, cache, tmp_path / "retrieval", config, split="dev")
    rows = jsonl(tmp_path / "retrieval/retrieval.jsonl")
    assert len(rows) == 4 and all(row["error"] is None for row in rows)
    reader = Reader()
    await run_answers(suite, tmp_path / "retrieval", tmp_path / "answers", config, reader)
    answers = jsonl(tmp_path / "answers/answers.jsonl")
    assert len(answers) == 8 and len(reader.requests) == 8
    assert all(row["answer"] and not row["error"] for row in answers)
    assert [row["arm"] for row in answers[:2]] == ["hybrid", "full"]
    assert [row["arm"] for row in answers[4:6]] == ["full", "hybrid"]
    with pytest.raises(FileExistsError):
        await run_answers(suite, tmp_path / "retrieval", tmp_path / "answers", config, reader)


@pytest.mark.asyncio
async def test_failures_are_recorded_not_silently_skipped(tmp_path):
    suite, config = sample(), settings()
    with closing(cache_for(tmp_path, suite)) as cache:
        run_retrieval(suite, cache, tmp_path / "retrieval", config, split="dev")
    class Broken:
        async def complete(self, *args):
            raise TimeoutError("fixture timeout")
    await run_answers(suite, tmp_path / "retrieval", tmp_path / "answers", config, Broken())
    answers = jsonl(tmp_path / "answers/answers.jsonl")
    assert len(answers) == 8 and all(row["error"] and row["answer"] is None for row in answers)


def official_fixture(tmp_path):
    path = tmp_path / "official-fixture.py"
    path.write_text("def eval_question_answering(qas, eval_key='prediction'):\n"
                    "    return [0.5 for q in qas], 0, [1 for q in qas]\n")
    return OfficialScorers(locomo=path, locomo_sha256=file_digest(path))


@pytest.mark.asyncio
async def test_scoring_is_blinded_pending_can_retry_without_regeneration(tmp_path):
    suite, config = sample(), settings()
    with closing(cache_for(tmp_path, suite)) as cache:
        run_retrieval(suite, cache, tmp_path / "retrieval", config, split="dev")
    reader = Reader()
    await run_answers(suite, tmp_path / "retrieval", tmp_path / "answers", config, reader)
    official = official_fixture(tmp_path)
    class Judge:
        count = 0
        async def complete(self, system, user, output_tokens):
            assert '"arm"' not in user and "hybrid" not in user
            self.count += 1
            return {"model": "fixture-reader", "effort": "max",
                    "text": "not a verdict" if self.count == 1 else "yes"}
    judge = Judge()
    await run_judgments(suite, tmp_path / "answers", tmp_path / "judge1", config, judge, official)
    first = jsonl(tmp_path / "judge1/judgments.jsonl")
    assert sum(r["status"] == "pending" for r in first) == 1
    report = build_report(suite, tmp_path / "retrieval", tmp_path / "report1", config,
                          answers=tmp_path / "answers", judgments=tmp_path / "judge1")
    assert not report["quality_final_ready"]
    await run_judgments(suite, tmp_path / "answers", tmp_path / "judge2", config, judge,
                        official, retry_from=tmp_path / "judge1")
    assert judge.count == 9 and len(reader.requests) == 8
    report = build_report(suite, tmp_path / "retrieval", tmp_path / "report2", config,
                          answers=tmp_path / "answers", judgments=tmp_path / "judge2")
    assert report["quality_final_ready"]
    assert report["semantic_accuracy"]["locomo/all"]["full"]["mean"] == 1
    assert report["official_locomo_f1"]["locomo/all"]["full"]["mean"] == .5


@pytest.mark.asyncio
async def test_execution_errors_score_zero_and_official_hash_cannot_change(tmp_path):
    suite, config = sample(), settings()
    with closing(cache_for(tmp_path, suite)) as cache:
        run_retrieval(suite, cache, tmp_path / "retrieval", config, split="dev")
    class Broken:
        async def complete(self, *args):
            raise TimeoutError("failure")
    await run_answers(suite, tmp_path / "retrieval", tmp_path / "answers", config, Broken())
    official = official_fixture(tmp_path)
    class NeverJudge:
        async def complete(self, *args):
            raise AssertionError("Execution errors must not be sent to judge")
    await run_judgments(suite, tmp_path / "answers", tmp_path / "judgments", config, NeverJudge(), official)
    result = build_report(suite, tmp_path / "retrieval", tmp_path / "report", config,
                          answers=tmp_path / "answers", judgments=tmp_path / "judgments")
    assert result["answer_failures"] == 8
    assert result["semantic_accuracy"]["locomo/all"]["hybrid"]["n"] == 2
    assert result["semantic_accuracy"]["locomo/all"]["hybrid"]["mean"] == 0
    with pytest.raises(ValueError, match="hash mismatch"):
        OfficialScorers(locomo=tmp_path / "official-fixture.py", locomo_sha256="0" * 64)


def test_pairing_averages_repeats_and_counts_independent_clusters():
    rows = []
    for question, group in (("q1", "conversation1"), ("q2", "conversation1"), ("q3", "conversation2")):
        for repeat in range(3):
            for arm, score in (("hybrid", 0), ("full", 1)):
                rows.append(dict(case_id=question, cluster_id=group, arm=arm, repeat=repeat, score=score))
    result = paired_interval(rows, "score", draws=100)
    assert result["paired_cases"] == 3 and result["clusters"] == 2
    assert result["full_minus_hybrid"] == 1 and result["ci95"] == [1, 1]


@pytest.mark.asyncio
async def test_changed_artifacts_and_wrong_model_fail_closed(tmp_path):
    suite, config = sample(), settings()
    with closing(cache_for(tmp_path, suite)) as cache:
        run_retrieval(suite, cache, tmp_path / "retrieval", config, split="dev")
    class WrongModel:
        async def complete(self, *args):
            return {"text": "answer", "model": "other", "effort": "low"}
    await run_answers(suite, tmp_path / "retrieval", tmp_path / "answers", config, WrongModel())
    assert all("identity" in row["error"] for row in jsonl(tmp_path / "answers/answers.jsonl"))
    path = tmp_path / "retrieval/retrieval.jsonl"
    path.write_text(path.read_text() + "{}\n")
    with pytest.raises(ValueError, match="artifact changed"):
        await run_answers(suite, tmp_path / "retrieval", tmp_path / "tampered", config, Reader())


def test_future_history_is_excluded_identically_from_both_arms(tmp_path):
    suite = sample()
    early = suite.cases[0].model_copy(update={"cutoff": suite.corpora[0].units[0].messages[0].at})
    suite = suite.model_copy(update={"cases": (early,)})
    with closing(cache_for(tmp_path, suite)) as cache:
        run_retrieval(suite, cache, tmp_path / "retrieval", settings(), split="dev")
    for row in jsonl(tmp_path / "retrieval/retrieval.jsonl"):
        assert not row["error"]
        assert row["candidate_evidence_ids"] == ["m0"]
        assert row["presented_ids"] == ["m0"]


@pytest.mark.asyncio
async def test_embedding_batch_validates_identity_and_preserves_unknown_usage(tmp_path):
    suite = sample()
    class Embedder:
        async def embed(self, texts):
            return {"model": "fixture", "space": "space", "dimensions": 2,
                    "vectors": [[2, 1] for _ in texts], "usage": None}
    with closing(VectorCache(tmp_path / "cache.db", model="fixture", dimensions=2, space="space", writable=True)) as cache:
        receipts = await populate(suite, cache, Embedder(), batch_size=2)
        assert len(receipts) == 3 and all(r["usage"] is None for r in receipts)
        assert await populate(suite, cache, Embedder()) == []


def test_missing_vectors_do_not_reuse_previous_corpus_index(tmp_path):
    suite = sample()
    missing = suite.corpora[0].model_copy(update={"id": "missing", "units": (
        Unit(id="absent", messages=(suite.corpora[0].units[0].messages[0].model_copy(
            update={"id": "absent", "text": "not in cache"}),)),)})
    cases = tuple(suite.cases[0].model_copy(update={"id": f"missing{i}", "corpus_id": "missing",
                                                "cluster_id": "missing", "gold_ids": ()}) for i in range(2))
    expanded = Suite(sources=suite.sources, corpora=(*suite.corpora, missing), cases=(*suite.cases, *cases))
    with closing(cache_for(tmp_path, suite)) as cache:
        run_retrieval(expanded, cache, tmp_path / "retrieval", settings(), split="dev")
    rows = jsonl(tmp_path / "retrieval/retrieval.jsonl")
    assert all(r["error"] for r in rows if r["case_id"].startswith("missing"))


@pytest.mark.asyncio
async def test_data_quarantines_remain_visible_but_are_not_algorithm_failures(tmp_path):
    suite, config = sample(), settings()
    invalid = suite.cases[0].model_copy(update={"id": "invalid", "data_issues": ("gold_after_question_time",)})
    suite = suite.model_copy(update={"cases": (*suite.cases, invalid)})
    with closing(cache_for(tmp_path, suite)) as cache:
        run_retrieval(suite, cache, tmp_path / "retrieval", config, split="dev")
    reader = Reader()
    await run_answers(suite, tmp_path / "retrieval", tmp_path / "answers", config, reader)
    assert len(reader.requests) == 8  # No requests for the two quarantined arms.
    result = build_report(suite, tmp_path / "retrieval", tmp_path / "report", config, answers=tmp_path / "answers")
    assert result["case_count"] == 3 and result["eligible_case_count"] == 2
    assert result["data_invalid_cases"] == {"invalid": ["gold_after_question_time"]}
    assert result["retrieval_failures"] == 0 and result["answer_failures"] == 0


@pytest.mark.asyncio
async def test_runtime_reader_validates_identity_and_never_supplies_tools():
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from eval.association.client import RuntimeClient
    from agent.plugin_composition.models import CHAT_MODELS, LLMResponse
    seen = []
    class Bound:
        descriptor = SimpleNamespace(model="fixture-reader", reasoning_effort="max")
        async def complete(self, request):
            seen.append(request)
            return LLMResponse(content="response")
    class Models:
        @asynccontextmanager
        async def independent_execution(self, **kwargs):
            assert kwargs == {"model_id": "registered-fixture", "reasoning_effort": "max"}
            yield SimpleNamespace(chat=lambda role: Bound())
    context = SimpleNamespace(require=lambda key: Models() if key == CHAT_MODELS else None)
    result = await RuntimeClient(context, settings(), model_id="registered-fixture").complete("system", "question", 128)
    assert result["text"] == "response" and result["usage"] is None
    assert seen[0].tools == () and seen[0].tool_choice == "none"
    Bound.descriptor = SimpleNamespace(model="other", reasoning_effort="max")
    with pytest.raises(ValueError, match="differs"):
        await RuntimeClient(context, settings(), model_id="registered-fixture").complete("system", "question", 128)
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_zero_eligible_cases_cannot_be_reported_as_final_quality(tmp_path):
    suite, config = sample(), settings()
    suite = suite.model_copy(update={"cases": (suite.cases[0].model_copy(
        update={"data_issues": ("gold_after_question_time",)}),)})
    with closing(cache_for(tmp_path, suite)) as cache:
        run_retrieval(suite, cache, tmp_path / "retrieval", config, split="dev")
    reader = Reader()
    await run_answers(suite, tmp_path / "retrieval", tmp_path / "answers", config, reader)
    await run_judgments(suite, tmp_path / "answers", tmp_path / "judgments", config, reader, OfficialScorers())
    report = build_report(suite, tmp_path / "retrieval", tmp_path / "report", config,
                          answers=tmp_path / "answers", judgments=tmp_path / "judgments")
    assert reader.requests == []
    assert report["eligible_case_count"] == 0 and not report["quality_final_ready"]


@pytest.mark.asyncio
async def test_embedding_cli_pilot_does_not_call_provider_for_holdout(tmp_path, monkeypatch):
    from argparse import Namespace
    from contextlib import asynccontextmanager
    from eval.association.__main__ import remote
    suite = sample()
    hidden_message = suite.corpora[0].units[0].messages[0].model_copy(
        update={"id": "holdout-message", "text": "HOLDOUT MUST NOT BE EMBEDDED"})
    hidden_corpus = Corpus(id="holdout-history", adapter="fixture", units=(Unit(id="hidden", messages=(hidden_message,)),))
    hidden_case = suite.cases[0].model_copy(update={"id": "holdout-question", "corpus_id": hidden_corpus.id,
                                                  "cluster_id": hidden_corpus.id, "split": "holdout",
                                                  "question": "HOLDOUT QUESTION", "gold_ids": ("holdout-message",)})
    suite = Suite(sources=suite.sources, corpora=(*suite.corpora, hidden_corpus), cases=(*suite.cases, hidden_case))
    requested = []
    class Embeddings:
        def embedding_descriptor(self):
            return {"model": "fixture", "space": "fixture-space", "dimensions": 2}
        async def embed(self, texts):
            requested.extend(texts)
            return {**self.embedding_descriptor(), "vectors": [[1, 0] for _ in texts]}
    @asynccontextmanager
    async def isolated(*args):
        yield Embeddings()
    monkeypatch.setattr("eval.association.client.runtime_client", isolated)
    args = Namespace(command="embed", split="dev", limit=1, config=tmp_path / "unused",
                     model_workspace=tmp_path / "unused-workspace", receipts=tmp_path / "receipts.jsonl",
                     cache=tmp_path / "vectors.db", batch_size=32)
    await remote(args, suite, settings())
    assert requested and all("HOLDOUT" not in text for text in requested)
    assert sum(text in {case.question for case in suite.cases} for text in requested) == 1


def test_pilot_balances_datasets_before_question_categories():
    suite = sample()
    cases = tuple(suite.cases[0].model_copy(update={
        "id": f"{dataset}:{i}", "dataset": dataset, "cluster_id": f"{dataset}:{i}",
        "category": str(i % categories),
    }) for dataset, categories in (("longmemeval", 6), ("locomo", 4)) for i in range(30))
    selected = select_cases(suite.model_copy(update={"cases": cases}), "dev", 24)
    assert sum(case.dataset == "longmemeval" for case in selected) == 12
    assert sum(case.dataset == "locomo" for case in selected) == 12
