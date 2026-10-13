"""Graph-span job context uses truthiness, then string coercion.

Missing keys are covered elsewhere. These cases are values that are present
but still dropped or rewritten: "", 0, whitespace, and non-string ids.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

import worker_app.langgraph_flow as lg

_RAW = '{"classification": "Legal", "summary": "Contract terms."}'


def _classify(state: dict):
    span = MagicMock()

    @contextmanager
    def fake_span(_name: str):
        yield span

    with patch.object(lg, "tracer") as tracer, patch.object(
        lg, "_ollama_generate", return_value=_RAW
    ) as gen:
        tracer.start_as_current_span.side_effect = fake_span
        out = lg.classify_node(state)

    tracer.start_as_current_span.assert_called_once_with("llm.graph.classify")
    attrs = {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}
    return out, attrs, gen


@pytest.mark.parametrize(
    ("job_id", "s3_key", "expect_job_attr", "expect_key_attr", "expect_generate_job_id"),
    [
        ("", "docs/a.txt", None, "docs/a.txt", None),
        ("job-1", "", "job-1", None, "job-1"),
        (0, "docs/a.txt", None, "docs/a.txt", None),
        ("job-1", 0, "job-1", None, "job-1"),
        (0, 0, None, None, None),
        ("", "", None, None, None),
    ],
)
def test_classify_node_omits_falsy_job_context_independently(
    job_id, s3_key, expect_job_attr, expect_key_attr, expect_generate_job_id
):
    out, attrs, gen = _classify(
        {
            "document_text": "Some doc",
            "attempts": 3,
            "job_id": job_id,
            "s3_key": s3_key,
        }
    )

    assert attrs["llm.stage"] == "worker.classify"
    assert attrs["llm.attempts"] == 3
    if expect_job_attr is None:
        assert "job_id" not in attrs
    else:
        assert attrs["job_id"] == expect_job_attr
    if expect_key_attr is None:
        assert "s3.key" not in attrs
    else:
        assert attrs["s3.key"] == expect_key_attr
    assert gen.call_args.kwargs["job_id"] == expect_generate_job_id
    assert out["classification"] == "Legal"
    assert out["summary"] == "Contract terms."
    assert out["attempts"] == 3
    assert out["job_id"] == job_id
    assert out["s3_key"] == s3_key


def test_classify_node_keeps_whitespace_job_context_without_stripping():
    out, attrs, gen = _classify(
        {
            "document_text": "Some doc",
            "job_id": " ",
            "s3_key": "\n",
        }
    )

    assert attrs["job_id"] == " "
    assert attrs["s3.key"] == "\n"
    assert attrs["llm.attempts"] == 0
    assert gen.call_args.kwargs["job_id"] == " "
    assert out["classification"] == "Legal"


def test_classify_node_stringifies_numeric_job_context():
    out, attrs, gen = _classify(
        {
            "document_text": "Some doc",
            "job_id": 42,
            "s3_key": 7,
        }
    )

    assert attrs["job_id"] == "42"
    assert attrs["s3.key"] == "7"
    assert gen.call_args.kwargs["job_id"] == "42"
    assert out["job_id"] == 42
    assert out["s3_key"] == 7
