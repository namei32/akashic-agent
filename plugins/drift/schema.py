"""Validated operator requests for the initiative producer's durable control."""

from pydantic import BaseModel, ConfigDict, Field


class EmptyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ControlRequest(EmptyRequest):
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    expected_version: int = Field(ge=0)


class ControlReceipt(EmptyRequest):
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    paused: bool
    version: int = Field(ge=1)
