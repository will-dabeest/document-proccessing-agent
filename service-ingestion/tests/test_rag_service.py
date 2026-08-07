from unittest.mock import patch

from app.rag_service import answer_question


def test_answer_question_no_context_returns_fixed_message():
    with patch("app.rag_service.retrieve_context", return_value=([], [], [])):
        out = answer_question("What is X?", llm_call=lambda p: "should not run")
    assert out["answer"] == "No relevant information found in uploaded documents."
    assert out["snippets"] == []


def test_answer_question_calls_llm_and_returns_snippets():
    docs = ["ctx line one", "ctx two"]
    metas = [{"file": "a.txt"}, {"file": "b.txt"}]
    captured = {}

    def llm_call(prompt: str) -> str:
        captured["prompt"] = prompt
        return "synthesized answer"

    with patch("app.rag_service.retrieve_context", return_value=(docs, metas, [])):
        out = answer_question("Q?", llm_call=llm_call)

    assert out["answer"] == "synthesized answer"
    assert "ctx line one" in captured["prompt"]
    assert "Q?" in captured["prompt"]
    assert len(out["snippets"]) == 2
    assert out["snippets"][0]["text"] == "ctx line one"
    assert out["snippets"][0]["metadata"] == {"file": "a.txt"}


def test_answer_question_prompt_joins_chunks_and_requires_context_only():
    docs = ["alpha chunk", "beta chunk"]
    metas = [{"file": "a.txt"}, {"file": "b.txt"}]
    captured = {}

    def llm_call(prompt: str) -> str:
        captured["prompt"] = prompt
        return "ok"

    with patch("app.rag_service.retrieve_context", return_value=(docs, metas, [0.1, 0.2])):
        answer_question("Where is beta?", llm_call=llm_call)

    prompt = captured["prompt"]
    assert "Answer the question using only the context below" in prompt
    assert "If the answer is not in the context, say no relevant information found." in prompt
    assert "alpha chunk\n---\nbeta chunk" in prompt
    assert prompt.index("Context:") < prompt.index("alpha chunk")
    assert prompt.index("beta chunk") < prompt.index("Question:")
    assert "Where is beta?" in prompt
