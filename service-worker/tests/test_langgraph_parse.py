import json

import pytest

import worker_app.langgraph_flow as lg


def test_parse_json_obj_bare_object():
    assert lg._parse_json_obj('{"classification":"A","summary":"B"}') == {
        "classification": "A",
        "summary": "B",
    }


def test_parse_json_obj_embedded_in_prose():
    raw = 'Prefix text {"classification":"X","summary":"Y"} trailing'
    assert lg._parse_json_obj(raw) == {"classification": "X", "summary": "Y"}


def test_parse_json_obj_malformed_raises():
    with pytest.raises(json.JSONDecodeError):
        lg._parse_json_obj("this is not json")


def test_parse_json_obj_keeps_brace_inside_string_and_later_keys():
    """A } inside a string must not end the match; keys after it still count.

    The extractor is greedy over braces. A first-} match would drop classification
    when the summary mentions a closing brace.
    """
    raw = '{"summary":"see } note","classification":"Legal"}'
    assert lg._parse_json_obj(raw) == {
        "summary": "see } note",
        "classification": "Legal",
    }


def test_parse_json_obj_extracts_single_object_from_array():
    """Models sometimes wrap the object in a one-element array plus prose."""
    raw = 'Result:\n[{"classification":"Legal","summary":"Wrapped by the model."}]'
    assert lg._parse_json_obj(raw) == {
        "classification": "Legal",
        "summary": "Wrapped by the model.",
    }


def test_parse_json_obj_multi_object_array_does_not_keep_first():
    """A two-element array is not a single object; do not silently keep the first label."""
    raw = (
        '[{"classification":"Legal","summary":"A"},'
        '{"classification":"Tech","summary":"B"}]'
    )
    with pytest.raises(json.JSONDecodeError):
        lg._parse_json_obj(raw)
