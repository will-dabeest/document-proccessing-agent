from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry import trace
from pydantic import BaseModel

from app.aws_clients import get_dynamodb_resource, get_s3_client
from app.config import settings
from app.ollama_client import generate_llama3
from app.publisher import publish_job_safe
from app.rag_service import answer_question, index_document
from app.url_import import UrlImportError, fetch_url_document
from shared.job_schema import JobMessage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _normalize_otlp_endpoint(raw: str) -> str:
    ep = raw.strip()
    for prefix in ("https://", "http://"):
        if ep.startswith(prefix):
            return ep[len(prefix) :]
    return ep


def _configure_tracing() -> None:
    try:
        resource = Resource.create({"service.name": "service-ingestion"})
        provider = TracerProvider(resource=resource)
        endpoint = _normalize_otlp_endpoint(settings.otlp_endpoint)
        exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
    except Exception:
        logger.warning("tracing_init_failed", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_tracing()
    app.state.config = {"model": settings.ollama_model}
    yield


app = FastAPI(title="Document ingestion", lifespan=lifespan)
FastAPIInstrumentor.instrument_app(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    s3 = get_s3_client()
    safe_name = Path(file.filename or "upload").name
    body = await file.read()
    buf = BytesIO(body)
    s3.upload_fileobj(buf, settings.bucket_name, safe_name)
    job = JobMessage(s3_key=safe_name)
    try:
        publish_job_safe(job, filename=safe_name)
    except Exception:
        raise HTTPException(status_code=502, detail="Upload stored but job publish failed")
    try:
        text = body.decode("utf-8")
        index_document(job.idempotency_key, safe_name, text)
    except UnicodeDecodeError:
        pass
    return {"status": "uploaded", "file": safe_name, "job_id": job.idempotency_key}


class ImportUrlRequest(BaseModel):
    url: str


@app.post("/import-url")
async def import_url(req: ImportUrlRequest):
    try:
        body, ext = await fetch_url_document(req.url, settings)
    except UrlImportError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail) from e

    s3_key = f"imports/{uuid.uuid4()}{ext}"
    s3 = get_s3_client()
    buf = BytesIO(body)
    s3.upload_fileobj(buf, settings.bucket_name, s3_key)
    job = JobMessage(s3_key=s3_key)
    try:
        publish_job_safe(job, filename=s3_key)
    except Exception:
        raise HTTPException(status_code=502, detail="URL content stored but job publish failed")
    try:
        text = body.decode("utf-8")
        index_document(job.idempotency_key, s3_key, text)
    except UnicodeDecodeError:
        pass
    return {"status": "imported", "file": s3_key, "job_id": job.idempotency_key}


class IndexRequest(BaseModel):
    job_id: str
    filename: str
    text: str


@app.post("/internal/index")
async def internal_index(req: IndexRequest):
    index_document(req.job_id, req.filename, req.text)
    return {"status": "indexed"}


class AskRequest(BaseModel):
    question: str


@app.post("/ask")
async def ask(req: AskRequest):
    return answer_question(req.question, llm_call=generate_llama3)


@app.get("/documents")
async def list_documents():
    table = get_dynamodb_resource().Table(settings.dynamodb_table)
    resp = table.scan(Limit=100)
    items = []
    for it in resp.get("Items", []):
        items.append(
            {
                "message_id": it.get("MessageId"),
                "status": it.get("Status"),
                "classification": it.get("Classification"),
                "summary": it.get("Summary"),
            }
        )
    return {"items": items}
