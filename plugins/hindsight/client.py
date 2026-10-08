from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx

from .config import Config


class HindsightError(RuntimeError):
    pass


class HindsightClient:
    """Bounded HTTP adapter; no implicit retain, reflect, or external redirects."""

    def __init__(
        self,
        config: Config,
        *,
        api_key: str = "",
        http: httpx.AsyncClient | None = None,
    ):
        self.config = config
        self._owns_http = http is None
        self.http = http or httpx.AsyncClient(
            timeout=config.timeout_seconds, follow_redirects=False
        )
        self._headers = {"Authorization": "Bearer " + api_key} if api_key else {}

    async def close(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    def _bank(self, bank: str) -> str:
        return f"/v1/{quote(self.config.tenant, safe='')}/banks/{quote(bank, safe='')}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, object] | None = None,
        missing: bool = False,
    ) -> dict[str, Any] | None:
        try:
            async with self.http.stream(
                method,
                self.config.base_url + path,
                json=body,
                headers=self._headers,
                timeout=self.config.timeout_seconds,
            ) as response:
                if missing and response.status_code == 404:
                    return None
                if not 200 <= response.status_code < 300:
                    # Never propagate response text: it can contain credentials or private prompts.
                    raise HindsightError(f"Hindsight HTTP {response.status_code}")
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > 2 * 1024 * 1024:
                        raise HindsightError("Hindsight 响应超过 2 MiB 上限")
                import json

                result = json.loads(chunks)
                if not isinstance(result, dict):
                    raise HindsightError("Hindsight 响应不是 JSON 对象")
                return result
        except HindsightError:
            raise
        except (httpx.HTTPError, ValueError) as error:
            raise HindsightError(
                "Hindsight 请求失败或响应格式无效：" + type(error).__name__
            ) from None

    async def submit(
        self, bank: str, operation_id: str, item: Mapping[str, object]
    ) -> None:
        result = await self._request(
            "POST",
            self._bank(bank) + "/memories",
            body={"items": [dict(item)], "async": True, "operation_id": operation_id},
        )
        if result is None or result.get("operation_id") != operation_id:
            raise HindsightError("服务未确认原 operation_id；禁止以新身份重试")

    async def operation(self, bank: str, operation_id: str) -> str | None:
        result = await self._request(
            "GET",
            self._bank(bank) + "/operations/" + quote(operation_id, safe=""),
            missing=True,
        )
        if result is None:
            return None
        status = result.get("status")
        if status not in {"pending", "processing", "completed", "failed", "cancelled"}:
            raise HindsightError("Hindsight operation 状态无效")
        return str(status)

    async def recall(
        self, bank: str, query: str, *, timestamp: str
    ) -> dict[str, Any] | None:
        return await self._request(
            "POST",
            self._bank(bank) + "/memories/recall",
            missing=True,
            body={
                "query": query,
                "budget": self.config.budget,
                "max_tokens": self.config.max_tokens,
                "query_timestamp": timestamp,
                "types": ["world", "experience", "observation"],
                "include": {"source_facts": {"max_tokens": self.config.max_tokens}},
                "trace": True,
            },
        )

    async def retry(self, bank: str, operation_id: str) -> None:
        await self._request(
            "POST",
            self._bank(bank) + "/operations/" + quote(operation_id, safe="") + "/retry",
        )
