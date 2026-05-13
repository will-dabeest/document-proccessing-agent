"""Shared SQS job message contract between ingestion and worker."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class JobMessage(BaseModel):
    s3_key: str
    idempotency_key: str = Field(default_factory=lambda: str(uuid4()))
    uploaded_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_json_dict(self) -> dict[str, Any]:
        return self.model_dump()


def parse_job_message(body: str) -> JobMessage:
    return JobMessage.model_validate_json(body)
