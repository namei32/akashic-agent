from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from agent.plugin_composition import Context
from agent.plugin_contracts import ContentPart
from agent.plugin_contracts.tools import TOOLS, CallSource, Result

from .query import DetailQuery, HistoryQuery, QueryError, object_value

api_version = 3
name = "lol_match_history"
version = "1.0.0"
desc = "国服 LOL 战绩查询，通过客户端桥接读取隐藏生涯账号的接口战绩"
inject = (TOOLS,)


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = False
    bridge_url: str = "http://127.0.0.1:22361"
    token_env: str = Field(
        default="AKASHIC_LOL_BRIDGE_TOKEN", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"
    )

    @field_validator("bridge_url")
    @classmethod
    def check_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("bridge_url 必须是没有凭据、路径或查询参数的 HTTP(S) 地址")
        if parsed.scheme == "http" and parsed.hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ValueError("远程桥接请使用 HTTPS，或通过隧道连接本机端口")
        _ = parsed.port
        return value.rstrip("/")


class QueryTool:
    idempotent = True

    def __init__(self, config: Config, action: str, model: type[BaseModel] | None):
        self.config, self.action, self.model = config, action, model

    async def prepare(
        self, arguments: Mapping[str, object], source: CallSource | None = None
    ) -> Mapping[str, object] | str:
        try:
            if self.model is None:
                return {} if not arguments else "状态查询不接受参数"
            return self.model.model_validate(dict(arguments)).model_dump()
        except ValidationError:
            return "查询参数无效；玩家需提供完整昵称#编号，查询场数为 1–50"

    async def invoke(self, key: str, arguments: Mapping[str, object]) -> Result:
        token = os.environ.get(self.config.token_env, "")
        if not token:
            payload = QueryError(
                "not_configured", f"请设置 {self.config.token_env} 并启动客户端桥接"
            ).payload()
        else:
            try:
                async with (
                    asyncio.timeout(40),
                    httpx.AsyncClient(
                        base_url=self.config.bridge_url,
                        trust_env=False,
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=40,
                    ) as client,
                ):
                    async with client.stream(
                        "GET" if self.action == "status" else "POST",
                        f"/v1/{self.action}",
                        json=None if self.action == "status" else dict(arguments),
                    ) as response:
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > 1024 * 1024:
                                raise QueryError(
                                    "response_too_large", "桥接返回结果超过大小限制"
                                )
                        payload = object_value(json.loads(data))
                        if "error" in payload:
                            failure = object_value(payload["error"])
                            if not isinstance(
                                failure.get("code"), str
                            ) or not isinstance(failure.get("message"), str):
                                raise QueryError(
                                    "invalid_response", "桥接错误缺少 code/message"
                                )
                        if response.status_code != 200 and "error" not in payload:
                            raise QueryError(
                                "bridge_error", f"桥接返回 HTTP {response.status_code}"
                            )
                        if "error" not in payload:
                            if self.action == "status" and (
                                payload.get("connected") is not True
                                or payload.get("protocol_version") != 1
                            ):
                                raise QueryError(
                                    "invalid_response", "桥接未确认客户端连接"
                                )
                            if self.action == "history" and (
                                not isinstance(payload.get("matches"), list)
                                or payload.get("returned_count")
                                != len(payload["matches"])
                                or payload.get("start") != arguments.get("start", 0)
                                or payload.get("requested_count")
                                != arguments.get("count", 20)
                            ):
                                raise QueryError(
                                    "invalid_response", "桥接未返回请求的战绩页"
                                )
                            if self.action == "detail" and object_value(
                                payload.get("match")
                            ).get("game_id") != arguments.get("game_id"):
                                raise QueryError(
                                    "invalid_response", "桥接未返回请求的对局"
                                )
                        _ = json.dumps(payload, allow_nan=False)
            except (httpx.TimeoutException, TimeoutError):
                payload = QueryError("timeout", "客户端桥接查询超时").payload()
            except httpx.RequestError:
                payload = QueryError(
                    "bridge_unavailable", "无法连接客户端桥接，请核对地址与运行状态"
                ).payload()
            except (ValueError, QueryError) as error:
                payload = (
                    error
                    if isinstance(error, QueryError)
                    else QueryError("invalid_response", "桥接未返回有效 JSON")
                ).payload()
        return Result(
            "error" if "error" in payload else "success",
            (
                ContentPart(
                    "text", json.dumps(payload, ensure_ascii=False, allow_nan=False)
                ),
            ),
        )

    async def query(self, key: str) -> Result | None:
        return None


async def apply(ctx: Context) -> None:
    config = Config.model_validate(ctx.config)
    if not config.enabled:
        return
    catalog = ctx.require(TOOLS)
    _ = await catalog.declare_group(ctx, description=desc)
    definitions = (
        ("status", "lol_client_status", "检查国服客户端桥接和登录状态。", None),
        (
            "history",
            "lol_match_history",
            "按昵称#编号查当前大区的近期战绩，包括隐藏生涯账号。SGP 优先，LCU 后备。"
            "hidden_history_verified 仅表示本次取得了 PRIVATE 账号的战绩；不能由它推断所有隐藏账号可查。",
            HistoryQuery,
        ),
        (
            "detail",
            "lol_match_detail",
            "使用战绩中的 game_id 查看当前大区单局双方玩家数据。",
            DetailQuery,
        ),
    )
    for action, tool_name, description, model in definitions:
        tool = QueryTool(config, action, model)

        @asynccontextmanager
        async def open_tool(
            _state: Mapping[str, object], value: QueryTool = tool
        ) -> AsyncIterator[QueryTool]:
            yield value

        _ = await catalog.register(
            ctx,
            name=tool_name,
            description=description,
            parameters=(
                model.model_json_schema()
                if model
                else {"type": "object", "properties": {}, "additionalProperties": False}
            ),
            open=open_tool,
            idempotent=True,
            parallel=True,
        )
