from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.chunking import chunk_text
from app.config import settings

logger = logging.getLogger(__name__)

_model: Any = None
_chroma_client: Any = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(settings.embedding_model_name)
    return _model


def _get_collection():
    global _chroma_client
    if _chroma_client is None:
        import chromadb

        persist = Path(settings.chroma_persist_dir)
        persist.mkdir(parents=True, exist_ok=True)
        _chroma_client = chromadb.PersistentClient(path=str(persist))
    return _chroma_client.get_or_create_collection("documents")


def index_document(job_id: str, filename: str, text: str) -> None:
    if not text.strip():
        return
    chunks = chunk_text(text)
    model = _get_model()
    embeddings = model.encode(chunks)
    collection = _get_collection()
    ids = [f"{job_id}-{i}" for i in range(len(chunks))]
    metadatas: list[dict[str, Any]] = [{"file": filename, "job_id": job_id} for _ in chunks]
    collection.add(
        documents=list(chunks),
        embeddings=embeddings.tolist(),
        ids=ids,
        metadatas=metadatas,
    )


def retrieve_context(question: str, n_results: int = 3) -> tuple[list[str], list[dict[str, Any]]]:
    model = _get_model()
    collection = _get_collection()
    q_emb = model.encode([question])[0].tolist()
    results = collection.query(
        query_embeddings=[q_emb],
        n_results=n_results,
    )
    docs = (results.get("documents") or [[]])[0]
    metas = (results.get("metadatas") or [[]])[0]
    return docs, metas


def answer_question(question: str, *, llm_call) -> dict[str, Any]:
    docs, metas = retrieve_context(question, n_results=3)
    if not docs:
        return {
            "answer": "No relevant information found in uploaded documents.",
            "snippets": [],
        }
    context = "\n---\n".join(docs)
    prompt = f"""Answer the question using only the context below. If the answer is not in the context, say no relevant information found.

Context:
{context}

Question:
{question}
"""
    answer = llm_call(prompt)
    snippets = [{"text": d, "metadata": m} for d, m in zip(docs, metas, strict=False)]
    return {"answer": answer, "snippets": snippets}
