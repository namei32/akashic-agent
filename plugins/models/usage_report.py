"""调用账的只读统计：字段未知独立计数，分页不缩小总计的范围。"""
from __future__ import annotations

import base64
import copy
import json
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, timedelta

from core.common.file_io import run_file_io
from .contract import ModelUsageQuery

TOKEN_FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens",
                "cache_write_input_tokens", "reasoning_output_tokens")
_PAGE_SIZE = 50


class UsageReports:
    """仅按需查询；短缓存有界且只含公开统计，随 Models Fiber 释放。"""

    def __init__(self, read: Callable[[ModelUsageQuery], dict[str, object]]) -> None:
        self._read = read
        self._lock = threading.Lock()
        self._cache: OrderedDict[ModelUsageQuery, tuple[float, dict[str, object]]] = OrderedDict()

    async def report(self, query: ModelUsageQuery) -> dict[str, object]:
        return await run_file_io(lambda: self._report(query))

    def _report(self, query: ModelUsageQuery) -> dict[str, object]:
        with self._lock:
            cached = self._cache.get(query)
            if cached is None or time.monotonic() - cached[0] >= 3:
                result = self._read(query)
                self._cache[query] = (time.monotonic(), result)
            else:
                result = cached[1]
            self._cache.move_to_end(query)
            while len(self._cache) > 8:
                self._cache.popitem(last=False)
            return copy.deepcopy(result)


def read_report(connection: sqlite3.Connection, query: ModelUsageQuery, *, has_attempt: bool) -> dict[str, object]:
    if query.start.utcoffset() is None or query.end.utcoffset() is None:
        raise ValueError("时间必须包含时区")
    start, end = query.start.astimezone(UTC), query.end.astimezone(UTC)
    if not timedelta(0) < end - start <= timedelta(days=31):
        raise ValueError("请选择不超过 31 天的时间范围")
    if query.model is not None and not 1 <= len(query.model) <= 256:
        raise ValueError("模型筛选无效")
    cursor = _decode_cursor(query.cursor)
    ceiling = cursor[2] if cursor is not None else connection.execute("SELECT COALESCE(MAX(rowid), 0) FROM model_calls").fetchone()[0]
    # 白名单投影不读取请求、响应、连接凭据或错误正文。
    fields = ", ".join(f"json_extract(usage_json, '$.{name}') AS {name}" for name in TOKEN_FIELDS)
    attempt = "attempt" if has_attempt else "NULL"
    source = f"""WITH calls AS (
        SELECT id, started_at, finished_at, state, first_token_ms, duration_ms, {attempt} AS attempt,
            json_extract(binding_json, '$.model') AS model,
            json_extract(usage_json, '$.coverage') AS coverage, {fields}
        FROM model_calls WHERE rowid <= ? AND julianday(started_at) >= julianday(?) AND julianday(started_at) < julianday(?)
    ), selected AS (SELECT * FROM calls WHERE (? IS NULL OR model = ?)) """
    parameters = (ceiling, start.isoformat(), end.isoformat(), query.model, query.model)
    counts = ", ".join(
        f"SUM({name}) AS {name}, COUNT({name}) AS {name}_known_calls" for name in TOKEN_FIELDS
    )
    invalid = " OR ".join(f"({name} IS NOT NULL AND (typeof({name}) != 'integer' OR {name} < 0))" for name in TOKEN_FIELDS)
    aggregates = f"""COUNT(*) AS calls, COALESCE(SUM({invalid}), 0) AS invalid_usage,
        COALESCE(SUM(state = 'success'), 0) AS success,
        COALESCE(SUM(state = 'error'), 0) AS errors,
        COALESCE(SUM(state = 'started'), 0) AS unsettled,
        COALESCE(SUM(attempt > 0), 0) AS retries, COUNT(attempt) AS attempt_known_calls,
        COALESCE(SUM(coverage = 'exact'), 0) AS exact_calls, {counts},
        AVG(first_token_ms) AS first_token_ms, COUNT(first_token_ms) AS first_token_known_calls,
        AVG(duration_ms) AS duration_ms,
        SUM(CASE WHEN state = 'success' AND duration_ms > 0 THEN output_tokens END) * 1000.0 /
            SUM(CASE WHEN state = 'success' AND duration_ms > 0 AND output_tokens IS NOT NULL THEN duration_ms END) AS tokens_per_second,
        SUM(CASE WHEN input_tokens > 0 THEN cached_input_tokens END) * 1.0 /
            SUM(CASE WHEN input_tokens > 0 AND cached_input_tokens IS NOT NULL THEN input_tokens END) AS cache_hit_rate,
        COUNT(CASE WHEN input_tokens > 0 AND cached_input_tokens IS NOT NULL THEN 1 END) AS cache_known_calls"""

    # 超时使整次查询明确失败，不能把被截断的扫描报告为完整统计。
    deadline = time.monotonic() + 5
    connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10_000)
    try:
        summary = dict(connection.execute(source + f"SELECT {aggregates} FROM selected", parameters).fetchone())
        if summary.pop("invalid_usage"):
            raise RuntimeError("模型调用账中存在无效 token 计数")
        models = [dict(row) for row in connection.execute(
            source + "SELECT model, COUNT(*) AS calls FROM calls GROUP BY model ORDER BY calls DESC, model", parameters)]
        bucket = "%Y-%m-%dT%H:00:00Z" if end - start <= timedelta(days=2) else "%Y-%m-%dT00:00:00Z"
        trend = [dict(row) for row in connection.execute(
            source + f"SELECT strftime(?, started_at) AS bucket, COUNT(*) AS calls, {counts} "
            "FROM selected GROUP BY bucket ORDER BY bucket", (*parameters, bucket))]
        rows = [dict(row) for row in connection.execute(
            source + "SELECT * FROM selected WHERE (? IS NULL OR (started_at, id) < (?, ?)) "
            "ORDER BY started_at DESC, id DESC LIMIT ?", (*parameters,
                None if cursor is None else cursor[0],
                None if cursor is None else cursor[0], None if cursor is None else cursor[1], _PAGE_SIZE + 1))]
    except sqlite3.OperationalError as error:
        if time.monotonic() >= deadline and "interrupted" in str(error):
            raise TimeoutError("统计查询超时，请缩短时间范围后重试") from error
        raise
    finally:
        connection.set_progress_handler(None, 0)
    next_cursor = _encode_cursor(rows[_PAGE_SIZE - 1], ceiling) if len(rows) > _PAGE_SIZE else None
    # SQLite 的默认时间没有 Z，公开出口显式标成 UTC，避免浏览器按本地时间解析。
    for row in rows:
        for name in ("started_at", "finished_at"):
            value = row[name]
            row[name] = None if value is None else str(value).replace(" ", "T") + "Z"
    return {"start": start.isoformat(), "end": end.isoformat(), "summary": summary, "models": models,
            "trend": trend, "bucket_seconds": 3600 if end - start <= timedelta(days=2) else 86400,
            "calls": rows[:_PAGE_SIZE], "next_cursor": next_cursor}


def _encode_cursor(row: dict[str, object], ceiling: int) -> str:
    return base64.urlsafe_b64encode(json.dumps([row["started_at"], row["id"], ceiling]).encode()).decode()


def _decode_cursor(value: str | None) -> tuple[str, str, int] | None:
    if value is None:
        return None
    try:
        if len(value) > 1024:
            raise ValueError
        parts = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        if (not isinstance(parts, list) or len(parts) != 3
                or not all(isinstance(item, str) and item for item in parts[:2])
                or type(parts[2]) is not int or not 0 <= parts[2] <= 9223372036854775807):
            raise ValueError
        return parts[0], parts[1], parts[2]
    except (ValueError, UnicodeError) as error:
        raise ValueError("调用分页游标无效") from error
