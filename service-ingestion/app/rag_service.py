from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from opentelemetry import trace

from app.chunking import chunk_text
from app.config import settings
from shared.llm_telemetry import (
    flatten_distances,
    log_llm_event,
    rag_distance_stats,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

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


def retrieve_context(
    question: str, n_results: int = 3
) -> tuple[list[str], list[dict[str, Any]], list[float]]:
    """Returns (documents, metadatas, distances_flat). Distances may be empty if Chroma omits them."""
    t0 = time.perf_counter()
    with tracer.start_as_current_span("rag.retrieve_context") as span_outer:
        span_outer.set_attribute("rag.n_results_requested", n_results)
        span_outer.set_attribute("rag.question_chars", len(question))
        span_outer.set_attribute("rag.embedding_model", settings.embedding_model_name)

        with tracer.start_as_current_span("rag.embed_query"):
            model = _get_model()
            q_emb = model.encode([question])[0].tolist()

        with tracer.start_as_current_span("rag.chroma.query"):
            collection = _get_collection()
            results = collection.query(
                query_embeddings=[q_emb],
                n_results=n_results,
                include=["documents", "metadatas", "distances"],
            )

        docs = (results.get("documents") or [[]])[0]
        metas = (results.get("metadatas") or [[]])[0]
        distances = flatten_distances(results.get("distances"))
        dmin, dmax, dmean = rag_distance_stats(distances)

        n_returned = len(docs)
        empty = n_returned == 0
        span_outer.set_attribute("rag.n_chunks_returned", n_returned)
        span_outer.set_attribute("rag.empty_retrieval", empty)
        if dmin is not None:
            span_outer.set_attribute("rag.distance_min", float(dmin))
            span_outer.set_attribute("rag.distance_max", float(dmax))
            span_outer.set_attribute("rag.distance_mean", float(dmean))

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        log_llm_event(
            logger,
            span="rag.retrieve_context",
            outcome="ok",
            latency_ms=round(elapsed_ms, 3),
            rag_n_results_requested=n_results,
            rag_n_chunks_returned=n_returned,
            rag_empty_retrieval=empty,
            rag_distance_min=dmin,
            rag_distance_max=dmax,
            rag_distance_mean=dmean,
            rag_question_chars=len(question),
        )
        return docs, metas, distances


def answer_question(question: str, *, llm_call) -> dict[str, Any]:
    docs, metas, _distances = retrieve_context(question, n_results=3)
    if not docs:
        log_llm_event(
            logger,
            span="rag.answer_question",
            outcome="empty_retrieval",
            rag_empty_retrieval=True,
            rag_question_chars=len(question),
        )
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
    t0 = time.perf_counter()
    answer = llm_call(prompt)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    log_llm_event(
        logger,
        span="rag.answer_question",
        outcome="ok",
        latency_ms=round(elapsed_ms, 3),
        rag_n_chunks_returned=len(docs),
        rag_empty_retrieval=False,
        llm_context_chars=len(context),
        rag_question_chars=len(question),
        llm_prompt_chars=len(prompt),
    )
    snippets = [{"text": d, "metadata": m} for d, m in zip(docs, metas, strict=False)]
    return {"answer": answer, "snippets": snippets}
