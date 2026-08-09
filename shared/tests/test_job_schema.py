import json

import pytest
from pydantic import ValidationError

from shared.job_schema import JobMessage, parse_job_message


def test_job_message_roundtrip():
    j = JobMessage(s3_key="a.pdf", idempotency_key="abc", uploaded_at="2024-01-01T00:00:00+00:00")
    s = j.model_dump_json()
    j2 = parse_job_message(s)
    assert j2.s3_key == "a.pdf"
    assert j2.idempotency_key == "abc"


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


def test_job_message_accepts_empty_idempotency_key():
    """Current shared contract allows empty idempotency_key (no min_length).

    The worker claims Dynamo with MessageId=idempotency_key, so concurrent
    empty-key jobs collide on the same claim row.
    """
    j = JobMessage(
        s3_key="a.txt",
        idempotency_key="",
        uploaded_at="2024-01-01T00:00:00+00:00",
    )
    assert j.idempotency_key == ""
    parsed = parse_job_message(j.model_dump_json())
    assert parsed.idempotency_key == ""
