from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from worker_app.processor import (
    download_object_bytes,
    extract_text_from_object,
    process_job_body,
    save_completed,
    try_claim_job,
)


def test_extract_text_plain_utf8():
    assert extract_text_from_object("notes.txt", b"hello world") == "hello world"


def test_extract_text_non_utf8_returns_empty():
    assert extract_text_from_object("binary.bin", b"\xff\xfe\xfd") == ""


def test_extract_text_pdf_delegates_to_pypdf():
    mock_page = MagicMock()
    mock_page.extract_text.return_value = "Extracted PDF line"
    mock_reader = MagicMock()
    mock_reader.pages = [mock_page]
    with patch("worker_app.processor.PdfReader", return_value=mock_reader):
        out = extract_text_from_object("report.pdf", b"%PDF-1.4 dummy")
    assert out == "Extracted PDF line"


@mock_aws
def test_try_claim_job_claimed_then_duplicate_inflight():
    boto3.client("dynamodb", region_name="us-east-1").create_table(
        TableName="ProcessLog",
        KeySchema=[{"AttributeName": "MessageId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "MessageId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table = boto3.resource("dynamodb", region_name="us-east-1").Table("ProcessLog")
    jid = "job-claim-1"
    assert try_claim_job(table, jid) == "claimed"
    assert try_claim_job(table, jid) == "duplicate_inflight"


@mock_aws
def test_try_claim_job_duplicate_done_after_completed():
    boto3.client("dynamodb", region_name="us-east-1").create_table(
        TableName="ProcessLog",
        KeySchema=[{"AttributeName": "MessageId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "MessageId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table = boto3.resource("dynamodb", region_name="us-east-1").Table("ProcessLog")
    jid = "job-done-1"
    save_completed(table, jid, "Legal", "All good")
    assert try_claim_job(table, jid) == "duplicate_done"


@mock_aws
def test_process_job_body_invokes_run_agent_save_and_notify():
    tbl = MagicMock()
    with patch(
        "worker_app.processor.download_object_bytes", return_value=b"bytes"
    ) as dl, patch(
        "worker_app.processor.extract_text_from_object", return_value="extracted"
    ), patch(
        "worker_app.processor.run_agent",
        return_value={"classification": "Tech", "summary": "Done"},
    ) as ra, patch(
        "worker_app.processor.save_completed"
    ) as sc, patch(
        "worker_app.processor.notify_index"
    ) as ni:
        process_job_body("job-x", "file.txt", tbl)

    dl.assert_called_once_with("file.txt")
    ra.assert_called_once()
    state = ra.call_args[0][0]
    assert state["document_text"] == "extracted"
    assert state["job_id"] == "job-x"
    assert state["s3_key"] == "file.txt"
    sc.assert_called_once_with(tbl, "job-x", "Tech", "Done")
    ni.assert_called_once_with("job-x", "file.txt", "extracted")


def test_process_job_body_empty_extract_skips_notify_index():
    tbl = MagicMock()
    with patch(
        "worker_app.processor.download_object_bytes", return_value=b""
    ) as dl, patch(
        "worker_app.processor.extract_text_from_object", return_value=""
    ), patch(
        "worker_app.processor.run_agent",
        return_value={"classification": "Unknown", "summary": "empty"},
    ) as ra, patch(
        "worker_app.processor.save_completed"
    ), patch(
        "worker_app.processor.notify_index"
    ) as ni:
        process_job_body("job-y", "empty.bin", tbl)

    dl.assert_called_once_with("empty.bin")
    assert ra.call_args[0][0]["document_text"] == "(empty)"
    ni.assert_not_called()


def test_download_object_bytes_reads_configured_bucket_and_returns_body():
    payload = b"DOC_MARKER_18e0_object_bytes\xff"
    s3 = MagicMock()

    def download_fileobj(bucket, key, buf):
        buf.write(payload)

    s3.download_fileobj.side_effect = download_fileobj
    with patch("worker_app.processor.get_s3_client", return_value=s3), patch(
        "worker_app.processor.settings.bucket_name", "coverage-bucket-18e0"
    ):
        out = download_object_bytes("reports/q1.txt")

    assert out == payload
    s3.download_fileobj.assert_called_once()
    bucket, key, buf = s3.download_fileobj.call_args.args
    assert bucket == "coverage-bucket-18e0"
    assert key == "reports/q1.txt"
    assert buf.getvalue() == payload


def test_download_object_bytes_propagates_missing_object():
    s3 = MagicMock()
    s3.download_fileobj.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "The specified key does not exist."}},
        "GetObject",
    )
    with patch("worker_app.processor.get_s3_client", return_value=s3), patch(
        "worker_app.processor.settings.bucket_name", "coverage-bucket-18e0"
    ):
        with pytest.raises(ClientError) as exc:
            download_object_bytes("reports/missing.txt")

    assert exc.value.response["Error"]["Code"] == "NoSuchKey"
    bucket, key, _buf = s3.download_fileobj.call_args.args
    assert bucket == "coverage-bucket-18e0"
    assert key == "reports/missing.txt"


def test_process_job_body_download_failure_does_not_complete_or_index():
    tbl = MagicMock()
    missing = ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "The specified key does not exist."}},
        "GetObject",
    )
    with patch(
        "worker_app.processor.download_object_bytes", side_effect=missing
    ) as dl, patch(
        "worker_app.processor.run_agent"
    ) as ra, patch(
        "worker_app.processor.save_completed"
    ) as sc, patch(
        "worker_app.processor.notify_index"
    ) as ni:
        with pytest.raises(ClientError) as exc:
            process_job_body("job-missing", "reports/q1.txt", tbl)

    assert exc.value.response["Error"]["Code"] == "NoSuchKey"
    dl.assert_called_once_with("reports/q1.txt")
    ra.assert_not_called()
    sc.assert_not_called()
    ni.assert_not_called()


@mock_aws
def test_save_completed_overwrites_item():
    boto3.client("dynamodb", region_name="us-east-1").create_table(
        TableName="ProcessLog",
        KeySchema=[{"AttributeName": "MessageId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "MessageId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table = boto3.resource("dynamodb", region_name="us-east-1").Table("ProcessLog")
    jid = "job-save-1"
    try_claim_job(table, jid)
    save_completed(table, jid, "Tech", "Summary text")
    item = table.get_item(Key={"MessageId": jid})["Item"]
    assert item["Status"] == "Completed"
    assert item["Classification"] == "Tech"
    assert item["Summary"] == "Summary text"
