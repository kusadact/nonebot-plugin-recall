import json
from typing import Any

from pydantic import BaseModel, Field, validator


class Config(BaseModel):
    """Plugin config loaded from NoneBot global config."""

    recall_group_whitelist: set[int] = Field(
        default_factory=set,
        description="Only these group ids will trigger anti-recall messages.",
    )

    @validator("recall_group_whitelist", pre=True)
    def _parse_recall_group_whitelist(cls, value: Any) -> set[int]:
        if value is None:
            return set()

        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return set()
            try:
                value = json.loads(raw)
            except Exception:
                parts = [item.strip() for item in raw.replace("，", ",").split(",")]
                return {int(item) for item in parts if item}

        if isinstance(value, (list, tuple, set)):
            return {int(item) for item in value}

        return {int(value)}

    class Config:
        extra = "ignore"
