import anyio
import httpx
import pytest

from app.config import Settings

from app.url_import import (
    UrlImportError,
    _classify_body,
    _parse_and_validate_url,
    fetch_url_document,
    raise_for_private_or_meta_hosts,
)


class FakeResponse:
    def __init__(self, status_code, headers=None, chunks=None, url="https://example.com/"):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []
        self._url = url

    async def aread(self):
        return b"".join(self._chunks)

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", self._url)
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("error", request=request, response=response)


class FakeStream:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *_exc):
        return False


class FakeAsyncClient:
    instances = []
    responses = []

    def __init__(self, *args, **kwargs):
        self.requests = []
        self.__class__.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def stream(self, method, url):
        self.requests.append((method, url))
        response = self.__class__.responses.pop(0)
        response._url = url
        return FakeStream(response)


@pytest.fixture
def url_import_settings():
    return Settings(
        url_import_enabled=True,
        url_import_max_bytes=5,
        url_import_max_redirects=1,
        url_import_timeout_seconds=0.1,
    )


@pytest.fixture
def fake_async_client(monkeypatch):
    FakeAsyncClient.instances = []
    FakeAsyncClient.responses = []
    monkeypatch.setattr("app.url_import.httpx.AsyncClient", FakeAsyncClient)
    return FakeAsyncClient


def test_parse_rejects_file_scheme():
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url("file:///etc/passwd")
    assert exc.value.status_code == 400


def test_parse_rejects_empty_url():
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url("   ")
    assert exc.value.status_code == 400


def test_parse_accepts_https():
    raw, host, port = _parse_and_validate_url("https://example.com/path?q=1")
    assert host == "example.com"
    assert "example.com" in raw
    assert port is None


def test_parse_rejects_invalid_port():
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url("https://example.com:not-a-port/path")
    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid port"


def test_raise_for_private_blocks_loopback():
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("127.0.0.1")
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_hostname_localhost():
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("localhost")
    assert exc.value.status_code == 403


def test_classify_pdf_magic_overrides_octet_stream():
    assert _classify_body("application/octet-stream", b"%PDF-1.4\n1 0 obj") == "pdf"


def test_classify_html():
    assert _classify_body("text/html; charset=utf-8", b"<html></html>") == "html"


def test_classify_unknown_binary():
    assert _classify_body("application/zip", b"PK\x03\x04") == "unknown_binary"


def test_html_to_plain_text_trafilatura_smoke():
    from app.url_import import _html_to_plain_text

    raw = b"<html><body><article><p>UniqueMarkerAlpha beta</p></article></body></html>"
    out = _html_to_plain_text(raw)
    assert b"UniqueMarkerAlpha" in out


def test_fetch_url_document_preserves_markdown_extension(
    fake_async_client, monkeypatch, url_import_settings
):
    monkeypatch.setattr(
        "app.url_import.raise_for_private_or_meta_hosts", lambda _host: None
    )
    fake_async_client.responses = [
        FakeResponse(
            200,
            headers={"content-type": "text/markdown; charset=utf-8"},
            chunks=[b"# Title\n"],
        )
    ]

    body, ext = anyio.run(
        fetch_url_document, "https://example.com/doc.md", url_import_settings
    )

    assert body == b"# Title\n"
    assert ext == ".md"
    assert fake_async_client.instances[0].requests == [
        ("GET", "https://example.com/doc.md")
    ]


def test_fetch_url_document_revalidates_redirect_target_before_following(
    fake_async_client, monkeypatch, url_import_settings
):
    validated_hosts = []

    def validate_host(host):
        validated_hosts.append(host)
        if host == "169.254.169.254":
            raise UrlImportError(403, "URL resolves to a disallowed address")

    monkeypatch.setattr(
        "app.url_import.raise_for_private_or_meta_hosts", validate_host
    )
    fake_async_client.responses = [
        FakeResponse(302, headers={"location": "http://169.254.169.254/latest"})
    ]

    with pytest.raises(UrlImportError) as exc:
        anyio.run(fetch_url_document, "https://example.com/start", url_import_settings)

    assert exc.value.status_code == 403
    assert validated_hosts == ["example.com", "169.254.169.254"]
    assert fake_async_client.instances[0].requests == [
        ("GET", "https://example.com/start")
    ]


def test_fetch_url_document_enforces_streamed_response_limit(
    fake_async_client, monkeypatch, url_import_settings
):
    monkeypatch.setattr(
        "app.url_import.raise_for_private_or_meta_hosts", lambda _host: None
    )
    fake_async_client.responses = [
        FakeResponse(
            200, headers={"content-type": "text/plain"}, chunks=[b"abc", b"def"]
        )
    ]

    with pytest.raises(UrlImportError) as exc:
        anyio.run(
            fetch_url_document, "https://example.com/large.txt", url_import_settings
        )

    assert exc.value.status_code == 413
    assert exc.value.detail == "Response body exceeds configured limit"


def test_fetch_url_document_maps_remote_http_errors(
    fake_async_client, monkeypatch, url_import_settings
):
    monkeypatch.setattr(
        "app.url_import.raise_for_private_or_meta_hosts", lambda _host: None
    )
    fake_async_client.responses = [FakeResponse(404)]

    with pytest.raises(UrlImportError) as exc:
        anyio.run(fetch_url_document, "https://example.com/missing", url_import_settings)

    assert exc.value.status_code == 502
    assert exc.value.detail == "Remote server returned 404"
