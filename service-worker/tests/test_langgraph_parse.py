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
