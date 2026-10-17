"""Shared RAG store: one embedding model and one Chroma client for index and query."""

import sys
from types import ModuleType
from unittest.mock import MagicMock

import app.rag_service as rag
from app.rag_service import index_document, retrieve_context


class _Row:
    def tolist(self):
        return [0.0, 0.0]


class _Encoded:
    def __init__(self, n_rows: int) -> None:
        self._n_rows = n_rows

    def tolist(self):
        return [[0.0, 0.0] for _ in range(self._n_rows)]

    def __getitem__(self, index):
        return _Row()


class _FakeModel:
    def __init__(self, name: str) -> None:
        self.name = name
        self.encoded: list[list[str]] = []

    def encode(self, texts):
        self.encoded.append(list(texts))
        return _Encoded(len(texts))


def _install_fakes(monkeypatch, constructed, clients, opened):
    class FakeClient:
        def __init__(self, path: str) -> None:
            self.path = path
            clients.append(self)

        def get_or_create_collection(self, name: str):
            col = MagicMock()
            col.query.return_value = {
                "documents": [[]],
                "metadatas": [[]],
                "distances": [[]],
            }
            opened.append((self, name))
            return col

    st = ModuleType("sentence_transformers")

    def sentence_transformer(name: str):
        model = _FakeModel(name)
        constructed.append(model)
        return model

    st.SentenceTransformer = sentence_transformer
    chroma = ModuleType("chromadb")
    chroma.PersistentClient = FakeClient
    monkeypatch.setitem(sys.modules, "sentence_transformers", st)
    monkeypatch.setitem(sys.modules, "chromadb", chroma)


def test_embedding_model_is_loaded_once_from_settings(monkeypatch):
    constructed = []
    _install_fakes(monkeypatch, constructed, [], [])
    monkeypatch.setattr(rag.settings, "embedding_model_name", "fixture-embedder")
    monkeypatch.setattr(rag, "_model", None)

    first = rag._get_model()
    second = rag._get_model()

    assert constructed == [first]
    assert first is second
    assert first.name == "fixture-embedder"


def test_index_and_retrieve_share_model_and_documents_collection(tmp_path, monkeypatch):
    persist = tmp_path / "nested" / "chroma"
    constructed = []
    clients = []
    opened = []
    _install_fakes(monkeypatch, constructed, clients, opened)
    monkeypatch.setattr(rag.settings, "embedding_model_name", "fixture-embedder")
    monkeypatch.setattr(rag.settings, "chroma_persist_dir", str(persist))
    monkeypatch.setattr(rag, "_model", None)
    monkeypatch.setattr(rag, "_chroma_client", None)

    index_document("job-1", "policy.txt", "policy text")
    docs, metas, distances = retrieve_context("where is the policy?", n_results=3)

    assert docs == []
    assert metas == []
    assert distances == []
    assert [model.name for model in constructed] == ["fixture-embedder"]
    assert constructed[0].encoded == [["policy text"], ["where is the policy?"]]
    assert [client.path for client in clients] == [str(persist)]
    assert persist.is_dir()
    assert opened == [(clients[0], "documents"), (clients[0], "documents")]


def test_chroma_persist_dir_can_be_opened_again(tmp_path, monkeypatch):
    persist = tmp_path / "again" / "chroma"
    clients = []
    opened = []
    _install_fakes(monkeypatch, [], clients, opened)
    monkeypatch.setattr(rag.settings, "chroma_persist_dir", str(persist))
    monkeypatch.setattr(rag, "_chroma_client", None)

    rag._get_collection()
    monkeypatch.setattr(rag, "_chroma_client", None)
    rag._get_collection()

    assert [client.path for client in clients] == [str(persist), str(persist)]
    assert [name for _client, name in opened] == ["documents", "documents"]
    assert persist.is_dir()
