from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Player(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    riot_id: str = Field(min_length=3, max_length=80)
    platform: str = "HN1"

    @field_validator("riot_id")
    @classmethod
    def identity(cls, value: str) -> str:
        if not re.fullmatch(r"[^#\r\n]+#[^#\s]+", value.strip()):
            raise ValueError("玩家名称需包含完整标签，例如 namei#15100")
        return value.strip()

    @field_validator("platform")
    @classmethod
    def platform_id(cls, value: str) -> str:
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9]{1,11}", value):
            raise ValueError("区服代号无效；艾欧尼亚为 HN1")
        return value

    @property
    def key(self) -> str:
        return self.platform + ":" + self.riot_id.casefold()


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool | None = None
    bridge_directory: str = ""
    players: tuple[Player, ...] = ()
    daily_time: str = "09:00"
    timezone: str = "Asia/Shanghai"
    window: str = "previous_day"
    analysis_enabled: bool = True

    @field_validator("daily_time")
    @classmethod
    def clock(cls, value: str) -> str:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("发送时间使用 HH:MM 格式")
        return value

    @field_validator("timezone")
    @classmethod
    def zone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator("window")
    @classmethod
    def period(cls, value: str) -> str:
        if value not in {"previous_day", "rolling_day"}:
            raise ValueError("汇总范围必须为前一天或截至发送时间的过去 24 小时")
        return value

    @model_validator(mode="after")
    def check(self):
        if len(self.players) > 10 or len({p.key for p in self.players}) != len(self.players):
            raise ValueError("最多关注 10 位玩家，不能重复")
        if self.bridge_directory and not Path(self.bridge_directory).is_absolute():
            raise ValueError("采集目录必须是绝对路径")
        if self.enabled and (not self.players or not self.bridge_directory):
            raise ValueError("请先设置 Windows 采集目录并添加关注玩家")
        return self

    def daily_period(self, now: datetime) -> tuple[datetime, datetime, datetime]:
        local = now.astimezone(ZoneInfo(self.timezone))
        due = datetime.combine(local.date(), time.fromisoformat(self.daily_time), local.tzinfo)
        end = (datetime.combine(local.date(), time.min, local.tzinfo)
               if self.window == "previous_day" else due)
        return due, end - timedelta(days=1), end
