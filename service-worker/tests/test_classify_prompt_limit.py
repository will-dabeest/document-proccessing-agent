"""The classify prompt keeps 8000 document characters and drops the rest.

An over-long document that only asserts a distant tail is absent still passes if
the limit moves by a few characters. These cases pin the fence: the 8000th
character is sent, and the 8001st is not. The boundary character is non-ASCII
so a UTF-8 byte slice of the same length would drop it.
"""

from unittest.mock import patch

import worker_app.langgraph_flow as lg

_KEPT = "字"
_DROPPED = "尾"


def _document_sent_to_model(document_text: str) -> str:
    prompts: list[str] = []

    def fake_generate(prompt: str, **_kwargs: object) -> str:
        prompts.append(prompt)
        return '{"classification": "Long", "summary": "ok"}'

    with patch.object(lg, "_ollama_generate", side_effect=fake_generate):
        out = lg.classify_node({"document_text": document_text, "attempts": 0})

    assert out["classification"] == "Long"
    assert len(prompts) == 1
    marker = "Document:\n"
    section = prompts[0].split(marker, 1)[1]
    assert section.endswith("\n")
    return section[:-1]


def test_classify_prompt_includes_the_8000th_character():
    document = ("a" * 7999) + _KEPT
    assert len(document) == 8000

    sent = _document_sent_to_model(document)

    assert sent == document
    assert sent.endswith(_KEPT)


def test_classify_prompt_drops_the_8001st_character():
    document = ("a" * 7999) + _KEPT + _DROPPED + ("b" * 20)
    assert document[7999] == _KEPT
    assert document[8000] == _DROPPED

    sent = _document_sent_to_model(document)

    assert sent == document[:8000]
    assert sent.endswith(_KEPT)
    assert _DROPPED not in sent
    assert "b" not in sent
