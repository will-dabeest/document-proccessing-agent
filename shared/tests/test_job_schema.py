import json
from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from shared.job_schema import JobMessage, parse_job_message


def test_job_message_roundtrip():
    j = JobMessage(s3_key="a.pdf", idempotency_key="abc", uploaded_at="2024-01-01T00:00:00+00:00")
    s = j.model_dump_json()
    j2 = parse_job_message(s)
    assert j2.s3_key == "a.pdf"
    assert j2.idempotency_key == "abc"


def test_job_message_defaults_generate_idempotency_and_uploaded_at():
    """Upload/import construct JobMessage(s3_key=...) and rely on default factories."""
    before = datetime.now(timezone.utc)
    j1 = JobMessage(s3_key="only-key.txt")
    j2 = JobMessage(s3_key="only-key.txt")
    after = datetime.now(timezone.utc)

    assert j1.s3_key == "only-key.txt"
    UUID(j1.idempotency_key)
    UUID(j2.idempotency_key)
    assert j1.idempotency_key != j2.idempotency_key

    uploaded = datetime.fromisoformat(j1.uploaded_at)
    assert uploaded.tzinfo is not None
    assert before <= uploaded <= after


def test_parse_job_message_invalid_json_raises():
    with pytest.raises(ValidationError):
        parse_job_message("not json")


def test_parse_job_message_wrong_shape_raises():
    with pytest.raises(ValidationError):
        parse_job_message(json.dumps({"foo": 1}))


def test_to_json_dict_has_publisher_fields():
    j = JobMessage(s3_key="doc.txt", idempotency_key="id-1", uploaded_at="2024-06-01T12:00:00+00:00")
    d = j.to_json_dict()
    assert d["s3_key"] == "doc.txt"
    assert d["idempotency_key"] == "id-1"
    assert d["uploaded_at"] == "2024-06-01T12:00:00+00:00"
