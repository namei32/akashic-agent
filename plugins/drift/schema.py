"""Validated operator requests for the initiative producer's durable control."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EmptyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ControlRequest(EmptyRequest):
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    expected_version: int = Field(ge=0)


class ControlReceipt(EmptyRequest):
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    paused: bool
    version: int = Field(ge=1)


class ProducerConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    auto_propose: bool = False
    interval_seconds: int = Field(default=1800, ge=60, le=604800)
    start_hour: int = Field(default=9, ge=0, le=23)
    end_hour: int = Field(default=22, ge=1, le=24)

    @model_validator(mode="after")
    def validate_hours(self) -> "ProducerConfig":
        if self.start_hour >= self.end_hour:
            raise ValueError("start_hour 必须早于 end_hour")
        return self


class ProducerRequest(EmptyRequest):
    auto_propose: bool
    interval_seconds: int = Field(ge=60, le=604800)
    start_hour: int = Field(ge=0, le=23)
    end_hour: int = Field(ge=1, le=24)
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    expected_input: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_hours(self) -> "ProducerRequest":
        ProducerConfig.model_validate(self.model_dump(exclude={"request_id", "expected_input"}))
        return self
