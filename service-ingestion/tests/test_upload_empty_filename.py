"""An empty multipart filename must not become an empty S3 key.

``None`` and ``""`` are different values. A guard written as
``filename if filename is not None else "upload"`` still stores ``""``,
so every nameless upload shares one object and the worker is given a
blank key. The handler must collapse both to the stable ``upload`` key.
"""

import asyncio
from io import BytesIO
from unittest.mock import MagicMock, patch

from fastapi import UploadFile


_BODY = b"DOC_MARKER_empty_filename"


def test_upload_empty_filename_stores_under_upload_key():
    from app.main import upload

    upload_file = UploadFile(file=BytesIO(_BODY), filename="")
    mock_s3 = MagicMock()
    with patch("app.main.get_s3_client", return_value=mock_s3), patch(
        "app.main.publish_job_safe"
    ) as publish, patch("app.main.index_document") as index:
        result = asyncio.run(upload(upload_file))

    assert result["status"] == "uploaded"
    assert result["file"] == "upload"
    assert result["file"] != ""
    assert mock_s3.upload_fileobj.call_args.args[2] == "upload"

    job = publish.call_args.args[0]
    assert job.s3_key == "upload"
    assert publish.call_args.kwargs == {"filename": "upload"}
    assert result["job_id"] == job.idempotency_key
    index.assert_called_once_with(job.idempotency_key, "upload", _BODY.decode("utf-8"))
