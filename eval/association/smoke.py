"""A deliberately labelled, offline wiring check, not an accuracy benchmark."""
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .embeddings import VectorCache
from .report import build_report
from .runner import run_answers, run_retrieval
from .schema import Case, Corpus, Evidence, Settings, Suite, Unit, write_json


class FixtureReader:
    async def complete(self, system, user, output_tokens):
        return {"model": "fixture-reader", "effort": "max", "text": "Offline wiring fixture only.",
                "usage": None, "tool_calls": []}


async def smoke(output: Path):
    output.mkdir(parents=True, exist_ok=False)
    at = datetime(2020, 1, 1, tzinfo=UTC)
    messages = tuple(Evidence(id=f"e{i}", session_id=f"s{i}", speaker="Example speaker", role="observation",
                              at=at + timedelta(days=i), text=text)
                     for i, text in enumerate(("Project Cedar is a pottery workshop.", "Cedar reservation is R8K2.",
                                              "R8K2 starts at 15:40 on Saturday.")))
    corpus = Corpus(id="fixture-history", adapter="fixture-observation-v1",
                    units=tuple(Unit(id=m.id, messages=(m,)) for m in messages))
    case = Case(id="fixture-question", dataset="locomo", corpus_id=corpus.id, cluster_id=corpus.id,
                split="dev", category="multi-hop", official_category=1,
                question="When does the Cedar workshop start?", cutoff=at + timedelta(days=4),
                answer="15:40 on Saturday", gold_ids=("e0", "e1", "e2"), gold_level="message")
    suite = Suite(sources={"kind": "OFFLINE_FIXTURE_NOT_BENCHMARK"}, corpora=(corpus,), cases=(case,))
    config = Settings(model="fixture-reader", embedding_model="fixture-embedding", dimensions=2, repeats=1)
    write_json(output / "suite.json", suite)
    write_json(output / "settings.json", config)
    with closing(VectorCache(output / "vectors.sqlite3", model=config.embedding_model,
                             dimensions=2, space="offline-fixture-v1", writable=True)) as cache:
        for unit in corpus.units:
            cache.put(unit.input_text, [1., 0.2])
        cache.put(case.question, [1., 0.2])
        run_retrieval(suite, cache, output / "retrieval", config, split="dev")
    await run_answers(suite, output / "retrieval", output / "answers", config, FixtureReader())
    build_report(suite, output / "retrieval", output / "report", config, answers=output / "answers")
    print(f"Offline fixture completed: {output / 'report' / 'report.md'}")
