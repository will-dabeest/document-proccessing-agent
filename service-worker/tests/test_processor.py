from unittest.mock import MagicMock, patch

import boto3
from moto import mock_aws

from worker_app.processor import (
    extract_text_from_object,
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
