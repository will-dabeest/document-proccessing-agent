from __future__ import annotations

import io
import logging
from io import BytesIO

from botocore.exceptions import ClientError
from pypdf import PdfReader

from worker_app.aws_clients import get_s3_client
from worker_app.config import settings
from worker_app.langgraph_flow import run_agent

logger = logging.getLogger(__name__)


def extract_text_from_object(key: str, body: bytes) -> str:
    lower = key.lower()
    if lower.endswith(".pdf"):
        reader = PdfReader(BytesIO(body))
        parts: list[str] = []
        for page in reader.pages:
            parts.append(page.extract_text() or "")
        return "\n".join(parts).strip()
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def download_object_bytes(key: str) -> bytes:
    s3 = get_s3_client()
    buf = io.BytesIO()
    s3.download_fileobj(settings.bucket_name, key, buf)
    return buf.getvalue()


def try_claim_job(table, job_id: str) -> str:
    """Returns 'claimed', 'duplicate_done', or 'duplicate_inflight'."""
    try:
        table.put_item(
            Item={"MessageId": job_id, "Status": "Processing"},
            ConditionExpression="attribute_not_exists(MessageId)",
        )
        return "claimed"
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code != "ConditionalCheckFailedException":
            raise
    resp = table.get_item(Key={"MessageId": job_id})
    item = resp.get("Item") or {}
    status = item.get("Status")
    if status == "Completed":
        return "duplicate_done"
    return "duplicate_inflight"


def save_completed(table, job_id: str, classification: str, summary: str) -> None:
    table.put_item(
        Item={
            "MessageId": job_id,
            "Status": "Completed",
            "Classification": classification,
            "Summary": summary,
        }
    )


def notify_index(job_id: str, filename: str, text: str) -> None:
    try:
        import httpx

        httpx.post(
            f"{settings.ingestion_base_url.rstrip('/')}/internal/index",
            json={"job_id": job_id, "filename": filename, "text": text},
            timeout=30.0,
        )
    except Exception as e:
        logger.warning("index_notify_failed: %s", e)


def process_job_body(job_id: str, s3_key: str, dynamo_table) -> None:
    body = download_object_bytes(s3_key)
    text = extract_text_from_object(s3_key, body)
    result = run_agent(
        {
            "document_text": text or "(empty)",
            "job_id": job_id,
            "s3_key": s3_key,
        }
    )
    save_completed(
        dynamo_table,
        job_id,
        result.get("classification", "Unknown"),
        result.get("summary", ""),
    )
    if text:
        notify_index(job_id, s3_key, text)
