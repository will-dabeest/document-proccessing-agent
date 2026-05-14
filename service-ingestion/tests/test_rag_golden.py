"""Golden-style regression: RAG retrieval wiring + answer shape (mocked, no real models)."""

from unittest.mock import patch

from app.rag_service import answer_question


def test_golden_rag_answer_uses_retrieved_context_and_metadata():
    docs = ["Kubernetes uses a control plane and worker nodes to schedule containers."]
    metas = [{"file": "k8s-notes.txt", "job_id": "job-golden-1"}]
    distances = [0.04]

    captured: dict[str, str] = {}

    def llm_call(prompt: str) -> str:
        captured["prompt"] = prompt
        return (
            "The document describes Kubernetes control plane and worker scheduling."
        )

    with patch(
        "app.rag_service.retrieve_context",
        return_value=(docs, metas, distances),
    ):
        out = answer_question(
            "What does the document say about Kubernetes scheduling?",
            llm_call=llm_call,
        )

    assert "Kubernetes" in captured["prompt"]
    assert "control plane" in captured["prompt"]
    assert "Kubernetes" in out["answer"]
    assert len(out["snippets"]) == 1
    assert out["snippets"][0]["text"] == docs[0]
    assert out["snippets"][0]["metadata"]["file"] == "k8s-notes.txt"
    assert out["snippets"][0]["metadata"]["job_id"] == "job-golden-1"
