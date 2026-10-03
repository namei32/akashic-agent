# Hybrid / Full memory comparison

An isolated fixed-reader experiment. It does not change the daily Agent or introduce a production retrieval mode.

- Hybrid: production tokenizer and raw dense/BM25 scoring, top 100 per channel, equal-weight RRF (k=60). It never builds MemoryCycle or a graph. Zero BM25 matches are excluded.
- Full: production MemoryCycle.commit and query_memory/select_hits, including original candidate ordering. Queries verify unchanged committed graph state.
- Shared: source units, embedding space, 12,000-character evidence budget and neutral renderer. Actual token usage is recorded separately.

This measures the memory engine and fixed reader, not online snapshot persistence, UI or a multi-step Agent loop. Build timings cover in-memory index/engine preparation. Query timings exclude cached embedding lookup and the read-only verification guard. Neither code can infer graph causality from this two-arm comparison alone.

## Local verification

Run from the repository root with its dependencies installed:

```sh
python -m pytest -q tests/test_association_evaluation.py tests/test_memory_ablation.py tests/test_eval_message_runtime.py
python -m eval.association smoke --out /tmp/akashic-hf-smoke-new
```

Output directories must be new. Smoke uses labelled synthetic vectors and a fixture reader, makes no API calls, and produces no benchmark answer-quality claim.

## Prepare datasets

```sh
python -m eval.association prepare \
  --lme-dev /absolute/path/longmemeval-dev.json \
  --lme-holdout /absolute/path/longmemeval-holdout.json \
  --locomo /absolute/path/locomo10.json \
  --out /absolute/path/new-experiment/suite.json
```

Either dataset can be supplied alone. LoCoMo splits whole conversations (2 dev, remaining holdout) with seed 20260923; holdout sampling is round-robin across conversations, up to 60 multi-hop, 20 single-hop, 20 temporal and 20 adversarial questions. Undersized groups stay undersized. Open-domain questions are excluded.

LongMemEval retains distinct occurrences of repeated source session IDs and a separate mapping for session labels. Consecutive user inputs and their assistant response form a unit; dangling inputs remain source fragments. Empty messages retain identity but provide no embedding or rendered evidence. LoCoMo maps each speaker-labelled utterance to an observed event, without inventing an assistant response. This explicit observation adapter differs from daily completed-turn ingestion. It uses supplied text only; image URLs, generated captions, observations and summaries are not ingested. Report that modality boundary.

### LoCoMo evidence annotation audit

The adapter preserves original messages, timestamps, questions, answers and evidence references. Exact IDs take precedence. Numeric zero-padding such as D30:05 may resolve to D30:5 only when exactly one real message has that numeric identity in the same conversation. Missing, malformed or ambiguous references are never guessed, dropped or replaced using answer content. Each question records its original source IDs, resolved targets and resolution reasons outside the retrievable corpus.

A question containing any unresolved reference still runs retrieval and answer evaluation against the unchanged reference answer. All evidence metrics for that question are null, including recall, complete coverage, Recall@5/@10 and gained/lost gold evidence; its partly valid label set is not substituted as a smaller denominator. Reports separately count evidence-eligible cases, list exclusions and normalized IDs, while retaining the full question in answer-quality denominators. This differs from time-policy data quarantines, which skip the entire question.

On upstream LoCoMo commit 3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376, the frozen split has two affected development questions: conv-50:q69 normalizes D30:05 to the existing D30:5; conv-47:q38 retains missing D4:36 and excludes evidence metrics only. The latter reference answer's job facts were separately checked against D13:3, D18:1 and D18:7. That content check does not substitute D13:3 as a replacement gold label; neither arm uses annotation labels to build memory or select evidence. The 120 selected holdout questions retain their original selection.

This audit changes serialized suites and evaluator source hashes. Prepare new suites and fresh outputs; keep any already-running experiment on its frozen checkout.

### Explicit time policy

Default --time-policy strict exposes only history at or before question time. Future gold evidence quarantines the question as gold_after_question_time: it remains in artifacts, receives no model call and is reported separately from algorithm failures. Quality reports give requested and eligible counts.

The existing 30/100 LongMemEval split has 1 development and 5 holdout questions with this issue. Do not silently shift times or describe the filtered subset as the complete benchmark. The explicit alternative --time-policy provided-history uses all supplied history, retains the original question time for the reader and records a separate retrieval cutoff. It is not strict causal replay and produces a different suite hash. Freeze this choice before scoring answers.

## Frozen settings

Every command accepts the same optional --settings JSON. Defaults match the plan:

```json
{"model":"gpt-5.6-sol","effort":"max","embedding_model":"text-embedding-v4","dimensions":1024,"budget_chars":12000,"candidate_limit":100,"rrf_k":60,"full_limit":40,"repeats":3,"output_tokens":4096,"timeout_s":300,"seed":20260923}
```

For the pilot use repeats=1 in a separate settings file. Do not change settings after retrieval.

## Embedding preparation

The real provider adapter uses the existing isolated evaluation template. Set AKASHIC_EVAL_TEMPLATE and AKASHIC_PLUGIN_HOME to evaluation-specific paths. Its marked model-only workspace must not be the live workspace. No reply, memory or proactive workers are started. Credentials belong to the configured model provider, not result files or settings JSON.

```sh
python -m eval.association embed --suite /absolute/path/suite.json \
  --split dev --limit 24 \
  --settings /absolute/path/settings.json --cache /absolute/path/vectors.sqlite3 \
  --config /absolute/path/evaluation-config.toml \
  --model-workspace /absolute/path/new-model-workspace \
  --receipts /absolute/path/new-embedding-receipts.jsonl
```

This command uses the configured embedding service. It requires an explicit split and embeds only the selected cases and their visible history; a pilot does not pay to prepare all holdout data. Later prepare holdout with --split holdout and a new receipt file. Cache entries bind exact content hashes to model/space/dimension, with finite and normalized-vector checks. Unknown usage stays null. Partial preparation can reuse verified vectors with a new receipt file; changed vectors cannot overwrite prior entries. Unrelated SQLite databases are refused. Retrieval never falls back to a live embedding request.

## Retrieval and fixed reader

```sh
python -m eval.association retrieve --suite /absolute/path/suite.json \
  --settings /absolute/path/settings.json --cache /absolute/path/vectors.sqlite3 \
  --split dev --limit 24 --out /absolute/path/new-pilot-retrieval
python -m eval.association answer --suite /absolute/path/suite.json \
  --settings /absolute/path/settings.json --retrieval /absolute/path/new-pilot-retrieval \
  --config /absolute/path/evaluation-config.toml \
  --model-workspace /absolute/path/new-answer-workspace --out /absolute/path/new-answers
```

Holdout uses --split holdout without --limit. Every planned pair/repeat retains its success or failure record. Tool schemas are empty; tool_choice is none; proposed tools are refused and never executed. The bound model identity and effort must match settings. There is no generate-until-correct retry.

Development limits balance datasets first, then rotate categories within each dataset. With enough data, a 24-question two-dataset pilot takes 12 questions from each. The reader requests a short answer without citation IDs so evidence identifiers do not distort official lexical F1; evidence provenance is retained in retrieval artifacts, not asserted to be the model's internal reasoning.

Each stage requires a new output directory and writes an input manifest plus completion receipt. Interrupted runs are incomplete. Source, settings, cache and evidence changes invalidate reuse. Build timings are present only when the index was constructed, not zero for reused indexes.

## Pinned scoring

Supply reviewed local copies of LongMemEval evaluate_qa.py and LoCoMo task_eval/evaluation.py with SHA-256. They provide get_anscheck_prompt and eval_question_answering. Install their dependencies in the isolated evaluation environment. Floating external code is not downloaded or executed automatically.

```sh
python -m eval.association judge --suite /absolute/path/suite.json \
  --settings /absolute/path/settings.json --answers /absolute/path/answers \
  --lme-rubric /absolute/path/evaluate_qa.py --lme-sha256 REVIEWED_SHA256 \
  --locomo-scorer /absolute/path/evaluation.py --locomo-sha256 REVIEWED_SHA256 \
  --config /absolute/path/evaluation-config.toml \
  --model-workspace /absolute/path/new-judge-workspace --out /absolute/path/new-judgments
```

Only relevant scorers are required. LoCoMo official answer F1 is separate from custom strict semantic/refusal judgments. Its missing-evidence recall default is never used. Invalid judge responses remain pending. --retry-from with the same answers/rubrics/settings and a new --out retries only pending judgments; it never regenerates answers or replaces completed judgments.

## Paired reporting

```sh
python -m eval.association report --suite /absolute/path/suite.json \
  --settings /absolute/path/settings.json --retrieval /absolute/path/retrieval \
  --answers /absolute/path/answers --judgments /absolute/path/judgments \
  --out /absolute/path/new-report
```

Answers and judgments are optional for retrieval-only reports. Outputs are paired-summary.json and report.md. Repeats are averaged within questions, then intervals resample history clusters. Datasets and message/session evidence levels stay separate. Pending judgments block final quality claims. Reports retain evidence gains/losses, data quarantines, execution failures, latency and token coverage. Token differences are not monetary savings. Fixed-reader runs cannot substantiate reductions in Agent tool calls.

## Type checking

The root configuration excludes eval. Check this package explicitly:

```sh
pyright --project eval/association/pyrightconfig.json --pythonpath /absolute/path/project-python
```

### Personal-memory confirmation (2026-09-25)

`persona.prepare_persona` imports frozen PersonaMem-v2 text CSV selections and
32k histories. It drops system persona dossiers, indexes only original dialogue,
and records a synthetic order clock (no real-time forgetting claim). All four
options are deterministically shuffled with the reviewed upstream method. The
reader follows the official MCQ instruction; `--persona-scorer/--persona-sha256`
loads only the reviewed extraction and exact-choice scoring methods. No judge
model call is made for MCQ scores. This dialogue-only retrieval evaluation is not
the official full-context leaderboard protocol.

The adapter keeps hidden snippets and preferences out of corpus/query text.
Snippet evidence is scored only when every label maps uniquely. `persona_pilot`
round-robins personas before selecting additional questions. Reports include
persona-cluster paired intervals and overlapping metadata strata.

`diagnostics.diagnose` observes the native retrieval ticket without modifying
production code and requires candidate scores/order and rendered context to match
the archived run exactly. Seed absence alone does not establish failure because
independent/direct routes can still recall an item. Native readout exclusions do
not identify one specific internal threshold.

### Public H/F batch (2026-10-03)

The current engine uses Consumption v2 and compact graph columns. Evaluation
imports its production tokenizer and native query_memory readout; no engine
parameters are changed. Model requests run through a private stdio process using
the validated frozen provider runtime. Both code identities and provider artifacts
are recorded. Neither process starts the production Agent.

Memora weekly indexes every original user/assistant message, excluding hidden
operations, share_memory labels, persona dossiers and evaluation metadata.
Calendar dates are retained; within-day order uses padded source IDs. Queries
observe the entire question date. Rubrics remain evaluation-only.

Shared feedback normalization reads visible dialogue and previous direct
retrieval evidence. Its targets must be shown original IDs and must pass the
production feedback resolver. H applies exact node inhibition and a fixed
one-rank-unit remember boost per production boost; F applies native feedback.
This is a component adaptation, not full Agent ToolCall lifecycle validation.

Public scoring uses the pinned official LoCoMo Python F1 and blind refusal
judgments. Memora uses the pinned official single-judge prompt and FAMA formula,
one frozen Sol call per rubric item, with a quoted-data guard and strict JSON.
It is not the official three-judge leaderboard protocol. Questions without
forgetting rubric items have no forgetting-success metric.

Answers resume only a unique prefix of the frozen schedule. Every failed
attempt is flushed before stopping; completed attempts are never regenerated.
Criterion judgments have their own ledger, so interruption does not lose prior
verdicts. Pending scoring may receive one separate retry pass on the same answers.
