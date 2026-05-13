import json

import pytest


@pytest.fixture
def mock_llm_env(monkeypatch):
    monkeypatch.setenv(
        "LLM_MOCK_JSON",
        json.dumps(
            {
                "classification": "Technical",
                "summary": "Introductory explanation of Kubernetes architecture.",
            }
        ),
    )
    import importlib

    import worker_app.config as cfg

    importlib.reload(cfg)
    import worker_app.langgraph_flow as lg

    importlib.reload(lg)
    yield
    monkeypatch.delenv("LLM_MOCK_JSON", raising=False)
    importlib.reload(cfg)
    importlib.reload(lg)


def test_langgraph_mock(mock_llm_env):
    from worker_app.langgraph_flow import run_agent

    text = (
        "Kubernetes uses a control plane and worker nodes to schedule containers."
    )
    out = run_agent({"document_text": text})
    assert out.get("classification") == "Technical"
    assert "Kubernetes" in (out.get("summary") or "")
