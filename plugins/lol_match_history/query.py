"""Windows 桥接使用的 LCU/SGP 查询；此模块不依赖 Akashic。"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

# 合区后的 platformId；来源与验证范围见 README。
HOSTS = {
    "HN1": "hn1-k8s-sgp.lol.qq.com",
    "HN10": "hn10-k8s-sgp.lol.qq.com",
    "NJ100": "nj100-sgp.lol.qq.com",
    "GZ100": "gz100-sgp.lol.qq.com",
    "CQ100": "cq100-sgp.lol.qq.com",
    "TJ100": "tj100-sgp.lol.qq.com",
    "TJ101": "tj101-sgp.lol.qq.com",
    "BGP2": "bgp2-k8s-sgp.lol.qq.com",
}
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class QueryError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    def payload(self) -> dict[str, object]:
        return {"error": {"code": self.code, "message": str(self)}}


class HistoryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    riot_id: str = Field(min_length=3, max_length=128, description="完整的昵称#编号")
    start: int = Field(default=0, ge=0, le=10000)
    count: int = Field(default=20, ge=1, le=50)

    @model_validator(mode="after")
    def check_riot_id(self) -> HistoryQuery:
        name, separator, tag = self.riot_id.rpartition("#")
        if not separator or not name.strip() or not tag.strip():
            raise ValueError("请提供完整的昵称#编号")
        self.riot_id = name.strip() + "#" + tag.strip()
        return self


class DetailQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    game_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$", description="战绩返回的对局 ID")


def object_value(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise QueryError("invalid_response", "上游未返回 JSON 对象")
    return value


def reject_constant(value: str) -> Any:
    raise ValueError(f"无效的 JSON 数值：{value}")


class Client:
    """每个请求重新读取 lockfile 和短期 token；凭据不返回给调用者。"""

    def __init__(self, lockfile: Path, platform: str):
        try:
            fields = lockfile.read_text(encoding="utf-8").strip().split(":")
        except OSError as error:
            raise QueryError(
                "client_unavailable", "无法读取 lockfile，请启动并登录国服客户端"
            ) from error
        if (
            len(fields) != 5
            or fields[0] != "LeagueClient"
            or fields[4] != "https"
            or not fields[2].isdigit()
            or not 1 <= int(fields[2]) <= 65535
            or not fields[3]
        ):
            raise QueryError("invalid_lockfile", "客户端 lockfile 格式无效")
        self.platform = platform
        self.deadline = time.monotonic() + 30
        # 仅本机 LCU 使用自签证书；公网 SGP 始终验证 TLS。
        self.local = httpx.Client(
            base_url=f"https://127.0.0.1:{fields[2]}",
            auth=("riot", fields[3]),
            verify=False,
            trust_env=False,
        )
        self.remote = httpx.Client(trust_env=False)

    def close(self) -> None:
        self.local.close()
        self.remote.close()

    def request(
        self, client: httpx.Client, method: str, path: str, **kwargs: Any
    ) -> Any:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise QueryError("timeout", "战绩查询超时")
        try:
            with client.stream(
                method, path, timeout=min(remaining, 10), **kwargs
            ) as response:
                if not 200 <= response.status_code < 300:
                    code = {
                        401: "session_expired",
                        403: "access_denied",
                        404: "not_found",
                        429: "rate_limited",
                    }.get(response.status_code, "upstream_unavailable")
                    raise QueryError(code, f"查询接口返回 HTTP {response.status_code}")
                data = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() > self.deadline:
                        raise QueryError("timeout", "战绩查询超时")
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise QueryError(
                            "response_too_large", "上游战绩超过大小限制，请减少查询场数"
                        )
                return json.loads(data, parse_constant=reject_constant)
        except httpx.TimeoutException as error:
            raise QueryError("timeout", "查询接口超时") from error
        except httpx.RequestError as error:
            raise QueryError(
                "upstream_unavailable", "无法连接客户端或战绩服务"
            ) from error
        except (ValueError, UnicodeError) as error:
            raise QueryError("invalid_response", "查询接口未返回有效 JSON") from error

    def lcu(self, path: str, **kwargs: Any) -> Any:
        return self.request(self.local, "GET", path, **kwargs)

    def sgp(self, path: str, **kwargs: Any) -> Any:
        tokens = object_value(self.lcu("/entitlements/v1/token"))
        token = tokens.get("accessToken")
        if not isinstance(token, str) or not token:
            raise QueryError("session_expired", "客户端尚未提供有效登录 token")
        return self.request(
            self.remote,
            "GET",
            f"https://{HOSTS[self.platform]}:21019{path}",
            headers={"Authorization": f"Bearer {token}"},
            **kwargs,
        )

    def current(self) -> dict[str, Any]:
        player = object_value(self.lcu("/lol-summoner/v1/current-summoner"))
        if not isinstance(player.get("puuid"), str) or not player["puuid"]:
            raise QueryError("not_logged_in", "客户端尚未登录有效的召唤师")
        return player

    def history(self, query: HistoryQuery) -> dict[str, object]:
        name, tag = query.riot_id.rsplit("#", 1)
        # POST 是固定的只读 alias lookup，不提供任意 LCU 代理。
        aliases = self.request(
            self.local,
            "POST",
            "/lol-summoner/v1/summoners/aliases",
            json=[{"gameName": name, "tagLine": tag}],
        )
        if not isinstance(aliases, list):
            raise QueryError("invalid_response", "玩家查询未返回数组")
        if len(aliases) != 1:
            raise QueryError(
                "player_not_found", "未找到唯一玩家，请核对昵称#编号和当前登录大区"
            )
        player = object_value(aliases[0])
        for field, expected in (("gameName", name), ("tagLine", tag)):
            actual = player.get(field)
            if actual and (
                not isinstance(actual, str) or actual.casefold() != expected.casefold()
            ):
                raise QueryError("invalid_response", "玩家查询返回的昵称#编号不匹配")
        puuid = player.get("puuid")
        if not isinstance(puuid, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", puuid
        ):
            raise QueryError("invalid_response", "玩家资料缺少有效 PUUID")
        attempts: list[dict[str, object]] = []
        try:
            raw = object_value(
                self.sgp(
                    f"/match-history-query/v1/products/lol/player/{puuid}/SUMMARY",
                    params={"startIndex": query.start, "count": query.count},
                )
            )
            games = raw.get("games")
            if not isinstance(games, list):
                raise QueryError("invalid_response", "SGP 战绩缺少 games 数组")
            if not games:
                raise QueryError("empty_page", "SGP 返回空页，继续尝试 LCU")
            if len(games) > query.count:
                raise QueryError("invalid_response", "SGP 返回的场数超过请求范围")
            matches = [
                match_value(object_value(item).get("json"), self.platform, puuid=puuid)
                for item in games
            ]
            source = "sgp"
        except QueryError as first:
            attempts.append({"source": "sgp", **first.payload()})
            try:
                raw = object_value(
                    self.lcu(
                        f"/lol-match-history/v1/products/lol/{puuid}/matches",
                        params={
                            "begIndex": query.start,
                            "endIndex": query.start + query.count - 1,
                        },
                    )
                )
                page = object_value(raw.get("games"))
                games = page.get("games")
                if not isinstance(games, list):
                    raise QueryError("invalid_response", "LCU 战绩缺少 games 数组")
                if (
                    len(games) > query.count
                    or (games and query.start > 49)
                    or (
                        games and page.get("gameIndexBegin", query.start) != query.start
                    )
                ):
                    raise QueryError(
                        "pagination_unavailable", "LCU 未返回请求页，不能用其他页代替"
                    )
                matches = [
                    match_value(game, self.platform, puuid=puuid, lcu_summary=True)
                    for game in games
                ]
                source = "lcu"
            except QueryError as second:
                return {
                    "error": {
                        "code": "history_unavailable",
                        "message": "SGP 和 LCU 均未取得战绩",
                    },
                    "attempts": [*attempts, {"source": "lcu", **second.payload()}],
                }
        privacy = player.get("privacy", "UNKNOWN")
        return {
            "player": {"riot_id": query.riot_id, "puuid": puuid, "privacy": privacy},
            "platform": self.platform,
            "source": source,
            "start": query.start,
            "requested_count": query.count,
            "returned_count": len(matches),
            "matches": matches,
            "hidden_history_verified": privacy == "PRIVATE" and bool(matches),
            "attempts": attempts,
            "observed_at": int(time.time()),
        }

    def detail(self, query: DetailQuery) -> dict[str, object]:
        attempts: list[dict[str, object]] = []
        try:
            raw = object_value(
                self.sgp(
                    f"/match-history-query/v1/products/lol/{self.platform}_{query.game_id}/SUMMARY"
                )
            )
            match = match_value(raw.get("json"), self.platform)
            source = "sgp"
        except QueryError as error:
            attempts.append({"source": "sgp", **error.payload()})
            try:
                raw = self.lcu(f"/lol-match-history/v1/games/{query.game_id}")
                match = match_value(raw, self.platform)
                source = "lcu"
            except QueryError as second:
                return {
                    "error": {
                        "code": "detail_unavailable",
                        "message": "SGP 和 LCU 均未取得对局",
                    },
                    "attempts": [*attempts, {"source": "lcu", **second.payload()}],
                }
        if match["game_id"] != query.game_id:
            raise QueryError("invalid_response", "查询接口返回了其他对局")
        return {
            "source": source,
            "platform": self.platform,
            "match": match,
            "attempts": attempts,
            "observed_at": int(time.time()),
        }


def match_value(
    value: Any, platform: str, *, puuid: str | None = None, lcu_summary: bool = False
) -> dict[str, object]:
    game = object_value(value)
    game_platform = game.get("platformId", platform)
    if str(game_platform).removeprefix("TENCENT_").upper() != platform:
        raise QueryError("wrong_platform", "战绩大区与桥接配置不同，请核对 --platform")
    participants = game.get("participants")
    if not isinstance(participants, list) or not participants:
        raise QueryError("invalid_response", "对局缺少玩家数据")
    identity_rows = game.get("participantIdentities", [])
    if not isinstance(identity_rows, list):
        raise QueryError("invalid_response", "对局的玩家身份数据无效")
    identities = {}
    for raw in identity_rows:
        item = object_value(raw)
        if type(item.get("participantId")) is not int:
            raise QueryError("invalid_response", "玩家身份缺少 participantId")
        identities[item["participantId"]] = object_value(item.get("player"))
    players = []
    for raw in participants:
        participant = object_value(raw)
        participant_id = participant.get("participantId")
        if identities and type(participant_id) is not int:
            raise QueryError("invalid_response", "对局玩家缺少 participantId")
        identity = (
            identities.get(participant_id, participant)
            if type(participant_id) is int
            else participant
        )
        if (
            puuid
            and identity.get("puuid") != puuid
            and not (
                lcu_summary and len(participants) == 1 and not identity.get("puuid")
            )
        ):
            continue
        stats = object_value(participant.get("stats", participant))
        players.append(
            {
                "puuid": identity.get("puuid"),
                "name": identity.get("riotIdGameName", identity.get("summonerName")),
                "champion_id": participant.get("championId"),
                "champion": participant.get("championName"),
                "team_id": participant.get("teamId"),
                **{
                    key: stats.get(key)
                    for key in (
                        "win",
                        "kills",
                        "deaths",
                        "assists",
                        "goldEarned",
                        "totalDamageDealtToChampions",
                        "totalMinionsKilled",
                        "neutralMinionsKilled",
                    )
                },
            }
        )
    if not players:
        raise QueryError("invalid_response", "对局未包含查询玩家，不能用其他玩家代替")
    game_id = game.get("gameId")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", str(game_id)):
        raise QueryError("invalid_response", "对局缺少有效 gameId")
    return {
        "game_id": str(game_id),
        "platform": platform,
        "queue_id": game.get("queueId"),
        "mode": game.get("gameMode"),
        "started_at_ms": game.get("gameCreation"),
        "duration_seconds": game.get("gameDuration"),
        "players": players,
    }
