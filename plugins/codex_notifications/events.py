"""通知只保存调用方明确提交的事件，不读取 Codex 的数据库或聊天文件。"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = 1
    bridge_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=256)
    turn_id: str = Field(min_length=1, max_length=256)
    event: Literal["Stop"] = "Stop"
    cwd: str = Field(max_length=4096)
    observed_at: str = Field(max_length=64)
    summary: str = Field(max_length=6000)
    truncated: bool = False

    @field_validator("bridge_id", "session_id", "turn_id", "cwd")
    @classmethod
    def plain_field(cls, value: str) -> str:
        if value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("身份与目录不能包含首尾空白或控制字符")
        return value

    @field_validator("observed_at")
    @classmethod
    def timestamp(cls, value: str) -> str:
        if datetime.fromisoformat(value).utcoffset() is None:
            raise ValueError("事件时间必须带时区")
        return value

    @property
    def identity(self) -> str:
        parts = [self.bridge_id, self.session_id, self.turn_id, self.event]
        return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()

    def text(self) -> str:
        # Stop 是一轮停止的事实，不升级为任务完成或测试成功。
        summary = self.summary or "Codex 没有附带最终回复。"
        suffix = "\n\n（回复较长，此处仅保留前 6000 字符。）" if self.truncated else ""
        return (f"Codex 有新结果 · 本轮回复已停止\n\n"
                f"目录：{self.cwd or '未提供'}\n"
                f"会话：{self.session_id}\n回合：{self.turn_id}\n"
                f"记录时间：{self.observed_at}\n\n{summary}{suffix}")


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
