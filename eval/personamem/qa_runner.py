from __future__ import annotations

import asyncio
import json
import logging
import time


from .dataset import PersonaMemInstance

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 180.0


def format_tool_trace(tool_chain: list[dict]) -> str:
    if not tool_chain:
        return "  (no tool calls)"

    lines: list[str] = []
    for step_i, group in enumerate(tool_chain, 1):
        lines.append(f"[step {step_i}]")
        text = str(group.get("text") or "")
        if text.strip():
            lines.append("assistant_text:")
            lines.append(text.rstrip())

        calls = group.get("calls") or []
        if not calls:
            lines.append("(no tool calls)")
            lines.append("")
            continue

        for call_i, call in enumerate(calls, 1):
            lines.append(f"tool_call {call_i}: {call.get('name', '?')}")
            lines.append(
                "arguments:"
                f"\n{json.dumps(call.get('arguments') or {}, ensure_ascii=False, indent=2)}"
            )
            final_arguments = call.get("final_arguments")
            if isinstance(final_arguments, dict) and final_arguments != call.get("arguments"):
                lines.append(
                    "final_arguments:"
                    f"\n{json.dumps(final_arguments, ensure_ascii=False, indent=2)}"
                )
            status = str(call.get("status") or "").strip()
            if status:
                lines.append(f"status: {status}")
            result = str(call.get("result") or "")
            lines.append("result:")
            lines.append(result.rstrip() or "(empty)")
        lines.append("")

    return "\n".join(lines).rstrip()


def _render_options(options: list[str]) -> str:
    lines = []
    for idx, option in enumerate(options):
        label = chr(ord("a") + idx)
        lines.append(f"({label}) {option}")
    return "\n".join(lines)


async def run_qa_instance(
    rt,
    instance: PersonaMemInstance,
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> dict:
    from eval.longmemeval.runtime import ask
    qa_prefix = instance.qa_session_key
    qa_key = qa_prefix
    tool_chain = []

    t0 = time.monotonic()
    error: str | None = None
    predicted = ""

    try:
        predicted, tool_chain, qa_key = await ask(rt,
            question=(
                f"{instance.question}\n\n"
                f"Options:\n{_render_options(instance.all_options)}\n\n"
                "Respond with the single best option label only, such as (a)."
            ),
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
        "gold_answer": instance.gold_label,
        "gold_option": instance.gold_option,
        "all_options": list(instance.all_options),
        "predicted_answer": predicted,
        "tool_chain": tool_chain,
        "elapsed_s": round(elapsed, 2),
        "error": error,
        "persona_id": instance.persona_id,
        "topic": instance.topic,
        "shared_context_id": instance.shared_context_id,
        "end_index_in_shared_context": instance.end_index_in_shared_context,
    }


__all__ = ["format_tool_trace", "run_qa_instance"]
