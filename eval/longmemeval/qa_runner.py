"""Phase 3: run the real agent loop for each QA pair."""

from __future__ import annotations

import asyncio
import logging
import time



from .dataset import LMEInstance
from .runtime import BenchmarkRuntime

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 180.0


_TOOL_EMOJI = {
    "recall_memory":    "🔍",
    "search_messages":  "🔎",
    "fetch_messages":   "📄",
    "memorize":         "💾",
    "web_search":       "🌐",
    "web_fetch":        "🌐",
    "shell":            "💻",
}
_DEFAULT_TOOL_EMOJI = "🔧"


def format_tool_trace(tool_chain: list[dict], *, width: int = 90) -> str:
    """Render tool_chain as a readable log block with emojis."""
    if not tool_chain:
        return "  (no tool calls)"

    lines = []
    for step_i, group in enumerate(tool_chain, 1):
        text = (group.get("text") or "").strip()
        calls = group.get("calls") or []

        if text:
            # Truncate long reasoning text
            preview = text[:300] + ("…" if len(text) > 300 else "")
            for ln in preview.splitlines():
                lines.append(f"  🧠 {ln}")

        for call in calls:
            name = call.get("name", "?")
            emoji = _TOOL_EMOJI.get(name, _DEFAULT_TOOL_EMOJI)
            args = call.get("arguments") or {}

            # Show the most informative argument
            arg_preview = ""
            for key in ("query", "ids", "source_ref", "source_refs", "command"):
                val = args.get(key)
                if val:
                    s = str(val)[:80]
                    arg_preview = f"{key}={s!r}"
                    break

            result_raw = str(call.get("result") or "")
            result_preview = result_raw[:120].replace("\n", " ")
            if len(result_raw) > 120:
                result_preview += "…"

            lines.append(f"  {emoji} {name}({arg_preview})")
            lines.append(f"     ↳ {result_preview}")

    return "\n".join(lines)


async def run_qa_instance(
    rt: BenchmarkRuntime,
    instance: LMEInstance,
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> dict:
    """Run one QA turn and return a result dict with tool trace."""
    from .runtime import ask
    qa_key = instance.qa_session_key
    tool_chain = []

    t0 = time.monotonic()
    error: str | None = None
    predicted = ""

    try:
        predicted, tool_chain, qa_key = await ask(rt,
            question=instance.question + "\n\n[Respond in English only. One sentence or short phrase.]",
            question_date=instance.question_date, session_prefix=qa_key, timeout_s=timeout_s)
    except asyncio.TimeoutError:
        error = f"timeout after {timeout_s}s"
        logger.warning("QA timeout: %s", instance.question_id)
    except Exception as exc:
        error = str(exc)
        logger.exception("QA error: %s", instance.question_id)

    elapsed = time.monotonic() - t0
    return {
        "qa_session_id": qa_key,
        "question_id": instance.question_id,
        "question_type": instance.question_type,
        "question": instance.question,
        "gold_answer": instance.answer,
        "predicted_answer": predicted,
        "tool_chain": tool_chain,
        "elapsed_s": round(elapsed, 2),
        "error": error,
    }
