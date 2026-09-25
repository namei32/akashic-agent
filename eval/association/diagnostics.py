"""Observe native retrieval boundaries without changing production algorithms."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import json
from unittest.mock import patch

from .embeddings import VectorCache
from .retrieval import Full, Hybrid, cycle_fingerprint, materialize, render
from .runner import append, jsonl
from .schema import Settings, digest, read_suite, write_json


def full_trace(index: Full, case, vector):
    tickets = []
    native = index.cycle.retrieve

    def observed(*args, **kwargs):
        ticket = native(*args, **kwargs)
        tickets.append(ticket)
        return ticket

    before = cycle_fingerprint(index.cycle)
    with patch.object(index.cycle, "retrieve", side_effect=observed):
        candidates, diagnostics = index.query(case, vector)
    assert len(tickets) == 1
    ticket = tickets[0]
    assert before == cycle_fingerprint(index.cycle)
    return candidates, diagnostics, {
        "seed": [{"node_id": node, "mass": float(mass)} for node, mass in ticket.evidence.seed],
        "native_completion": [{"node_id": item.node_id, "score": item.score,
                               "sources": list(item.sources), "basin_ids": list(item.basin_ids)}
                              for item in ticket.completion.items],
        "direct_dense_nodes": [item["node_id"] for item in candidates if item["lane"] == "dense"],
        "persistent_state_unchanged": True,
    }


def loss_rows(case, corpus, turns, candidates, material, trace, arm):
    # Materialization may skip completely empty units: derive identities from actual turns.
    unit_nodes = {turn.turn_id: turn.node_id for turn in turns}
    nodes = {m.id: unit_nodes.get(unit.id) for unit in corpus.units for m in unit.messages}
    selected = {item["node_id"] for item in candidates}
    seeded = {item["node_id"] for item in trace.get("seed", [])}
    native = {item["node_id"] for item in trace.get("native_completion", [])}
    presented = set(material["presented_ids"])
    messages = {m.id: m for u in corpus.units for m in u.messages}
    output = []
    for gold in dict.fromkeys(case.gold_ids):
        matching = ([gold] if case.gold_level == "message" else
                    [m.id for m in messages.values() if (m.source_session_id or m.session_id) == gold])
        possible = {node for identity in matching if (node := nodes.get(identity)) is not None}
        if not possible:
            reason = "source_label_missing_or_empty"
        elif presented.intersection(matching):
            reason = "presented"
        elif selected & possible:
            reason = "character_budget"
        elif arm == "hybrid":
            reason = "outside_dense_bm25_candidate_union"
        elif native & possible:
            reason = "native_selection_limit"
        elif seeded & possible:
            reason = "seeded_but_not_native_readout"
        else:
            reason = "not_seeded_and_not_native_readout"
        output.append({"gold_id": gold, "node_ids": sorted(possible), "reason": reason,
                       "seeded": bool(seeded & possible), "native_completion": bool(native & possible),
                       "selected": bool(selected & possible),
                       "candidate_ranks": [i + 1 for i, item in enumerate(candidates)
                                           if item["node_id"] in possible]})
    return output


def diagnose(suite_path: Path, cache_path: Path, retrieval_path: Path, output: Path):
    suite = read_suite(suite_path)
    original = jsonl(retrieval_path / "retrieval.jsonl")
    manifest = json.loads((retrieval_path / "manifest.json").read_text())
    settings = Settings.model_validate(manifest["settings"])
    cases = {c.id: c for c in suite.cases}
    corpora = {c.id: c for c in suite.corpora}
    original_map = {(r["case_id"], r["arm"]): r for r in original}
    output.mkdir(parents=True, exist_ok=False)
    cache = VectorCache(cache_path, model=settings.embedding_model, dimensions=settings.dimensions)
    active_key, active, counts, verified = None, {}, Counter(), 0
    try:
        with (output / "retrieval-loss-ledger.jsonl").open("x") as stream:
            for identity in manifest["case_ids"]:
                case = cases[identity]
                corpus = corpora[case.corpus_id].visible(case.cutoff)
                key = digest(corpus)
                if key != active_key:
                    turns, state = materialize(corpus, cache)
                    active, active_key = {}, key
                for arm in ("hybrid", "full"):
                    if arm not in active:
                        active[arm] = Hybrid(turns, settings) if arm == "hybrid" else Full(turns, state, settings)
                    if arm == "full":
                        candidates, diagnostics, trace = full_trace(active[arm], case, cache.get(case.question))
                    else:
                        candidates, diagnostics = active[arm].query(case, cache.get(case.question))
                        trace = {name: [{"node_id": item["node_id"], "rank": item["ranks"][name]}
                                        for item in candidates if name in item["ranks"]]
                                 for name in ("dense", "bm25")}
                    material = render(corpus, candidates, settings.budget_chars)
                    old = original_map[identity, arm]
                    assert old["error"] is None
                    assert candidates == old["candidates"], (identity, arm, "candidate drift")
                    assert all(material[k] == old[k] for k in material), (identity, arm, "presentation drift")
                    losses = loss_rows(case, corpus, turns, candidates, material, trace, arm)
                    counts.update((case.dataset, arm, item["reason"]) for item in losses)
                    append(stream, {"case_id": identity, "dataset": case.dataset, "category": case.category,
                                    "arm": arm, "trace": trace, "gold": losses,
                                    "unchanged_candidates_and_presentation": True,
                                    "evidence_scorable": case.evidence_scorable,
                                    "candidate_count": len(candidates), "context_chars": material["context_chars"]})
                    verified += 1
                print(json.dumps({"case_id": identity, "verified_rows": verified}), flush=True)
        write_json(output / "summary.json", {"verified_rows": verified, "reader_calls": 0,
                   "counts": [{"dataset": d, "arm": a, "reason": r, "gold_items": n}
                              for (d, a, r), n in sorted(counts.items())],
                   "limits": "Seed absence alone is not failure: independent/direct routes can still retrieve. "
                   "Native-readout exclusion does not isolate a specific internal threshold."})
    finally:
        cache.close()
