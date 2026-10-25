from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import time

from .schema import Suite, canonical, digest, file_digest, jsonl, write_json
from .scoring import request_id, verdict
from .support import begin, source_identity


async def run_calls(
    suite: Suite,
    settings,
    h,
    requests: list[dict],
    output: Path,
    *,
    client=None,
    judging: bool = False,
    input_hash: str,
):
    begin(
        output,
        {
            "kind": "judge" if judging else "answer",
            "suite_sha256": digest(suite),
            "settings": settings.model_dump(mode="json"),
            "source": source_identity(h),
            "input_sha256": input_hash,
            "schedule_sha256": digest(requests),
            "planned_calls": len(requests),
            "mode": "live" if client else "requests-only",
            "tool_budget": 0,
            "output_limit": "Requested parameter; frozen provider does not enforce a hard output cap",
        },
    )
    path = output / ("judgments.jsonl" if judging else "answers.jsonl")
    if client is None:
        write_json(output / "requests.json", requests)
        write_json(
            output / "completion.json",
            {
                "requests_only": True,
                "requests": len(requests),
                "model_attempts": 0,
                "sha256": file_digest(output / "requests.json"),
            },
        )
        return
    cases = {c.id: c for c in suite.cases}
    old = jsonl(path) if path.exists() else []
    keys = [request_id(r) for r in requests]
    if [r["request_id"] for r in old] != keys[: len(old)]:
        raise ValueError(
            "Saved attempts are not a unique prefix of the frozen request schedule"
        )
    intents = output / "attempts.jsonl"
    started = jsonl(intents) if intents.exists() else []
    pending = [
        r for r in started if r["request_id"] not in {row["request_id"] for row in old}
    ]
    if pending:
        if (
            len(pending) != 1
            or len(old) >= len(requests)
            or pending[0]["request_id"] != keys[len(old)]
        ):
            raise ValueError(
                "Interrupted attempt does not match the next frozen request"
            )
        request = requests[len(old)]
        if pending[0]["request_sha256"] != digest(request):
            raise ValueError("Interrupted request content changed")
        # A process can disappear after provider completion but before result fsync.
        # Preserve the uncertain attempt as failed rather than sample a new answer.
        recovered = {
            **request,
            "request_id": request_id(request),
            "request_sha256": digest(request),
            "text": None,
            "usage": None,
            "score": None,
            "error": "InterruptedModelAttempt: provider completion unknown; answer was not regenerated",
        }
        with path.open("a") as stream:
            stream.write(canonical(recovered) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        old.append(recovered)
    if (output / "completion.json").exists():
        receipt = json.loads((output / "completion.json").read_text())
        if len(old) != len(requests) or receipt["sha256"] != file_digest(path):
            raise ValueError("Completed model records changed")
        return
    failures = sum(bool(r["error"]) for r in old)
    consecutive = 0
    with path.open("a") as stream:
        for request in requests[len(old) :]:
            row = {
                **request,
                "request_id": request_id(request),
                "request_sha256": digest(request),
                "text": None,
                "usage": None,
                "error": None,
                "score": None,
            }
            caught = None
            try:
                if request["input_error"]:
                    # Execution failure remains in the denominator; no replacement answer.
                    row["error"], row["score"] = request["input_error"], 0
                    failures += 1
                else:
                    with intents.open("a") as journal:
                        journal.write(
                            canonical(
                                {
                                    "request_id": request_id(request),
                                    "request_sha256": digest(request),
                                }
                            )
                            + "\n"
                        )
                        journal.flush()
                        os.fsync(journal.fileno())
                    start = time.perf_counter()
                    result = await asyncio.wait_for(
                        client.complete(
                            request["system"], request["user"], settings.output_tokens
                        ),
                        settings.timeout_s,
                    )
                    row["model_ms"] = (time.perf_counter() - start) * 1000
                    row.update(
                        text=result["text"],
                        usage=result.get("usage"),
                        model=result["model"],
                        effort=result["effort"],
                        call_record_id=result.get("call_record_id"),
                    )
                    if not str(row["text"]).strip() or result.get("tool_calls"):
                        raise ValueError(
                            "Fixed reader/judge returned empty text or a tool call"
                        )
                    if judging:
                        row.update(
                            verdict(
                                suite, cases[row["case_id"]], row["metric"], row["text"]
                            )
                        )
                    consecutive = 0
            except BaseException as error:
                caught = error
                failures += 1
                consecutive += 1
                row["error"] = f"{type(error).__name__}: {error}"
            stream.write(canonical(row) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            if caught and (
                isinstance(caught, (asyncio.TimeoutError, h.client.RemoteProtocolError))
                or not isinstance(caught, Exception)
                or "model identity" in str(caught).lower()
                or consecutive >= 3
            ):
                raise RuntimeError(
                    "Recorded model failure; stopped without regenerating completed attempts"
                ) from caught
    write_json(
        output / "completion.json",
        {
            "rows": len(requests),
            "failures": failures,
            "model_attempts": sum(r["input_error"] is None for r in requests),
            "pending": sum(r["score"] is None for r in jsonl(path)) if judging else 0,
            "sha256": file_digest(path),
        },
    )
