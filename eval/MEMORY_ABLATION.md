# Memory-access comparison

Run the same development questions with `akasha.retrieval_mode` set to `none`,
`dense`, and `full`. Defaults preserve production full retrieval. This changes
query evidence only; historical learning and original messages remain identical.
The `none` arm exposes no past messages to the answer model even though history
is prepared in its isolated workspace. It is not a measurement of the minimum
storage or startup cost of a memory-free agent.

Dense retrieval ranks normalized user/assistant turn vectors by maximum cosine
similarity, respects explicit forgetting, and skips sparse/graph retrieval.
Full retrieval retains the original five dense seeds plus graph completion.
All arms use the same context and tool-result character budgets; dense candidate
selection and ordering are intentionally a cosine-only baseline. This is a
comparison of retrieval policies, not an isolated estimate of every graph subcomponent.

Set `reply.allowed_tool_names` to `["recall_memory", "tool_search"]` in every arm.
Both schema discovery and actual execution receive the restricted tool view.
Shell, filesystem reads, direct database queries, memory writes and unrelated tools
cannot bypass the assigned retrieval policy through this reply path.

`python -m eval.memory_ablation` runs one trial. It replays original history into
a fresh workspace and may seed **historical** vectors from a read-only, fixed
prior calibration database. It validates space, dimensions, message identity and
text hash, then writes through MessageEmbeddings. Prior QA, answers, graphs and
model calls are not copied. All arms rebuild the same graph; costs reflect this
controlled harness and warm vector cache, not independently optimized systems.

The coordinator must register source/plugin/config/data hashes, exact question
IDs, arm order, model and budgets before running. Use Sol / max for both answer
and judge. Preserve all failures and attempts. LongMemEval uses pinned official
rubrics; PersonaMem uses exact option labels. Use separate QA sessions excluded
from learning and verify actual recall modes, tool names, clocks and graph nodes.

Scope: isolated experimental runtime only. Daily checkout, service, workspace,
plugin home and prior experiment records are protected. Local fixes and newly
created outputs are reversible; no remote deployment or external messaging.
