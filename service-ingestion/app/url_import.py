"""Fetch a single URL and produce bytes + file extension for S3 upload (HTML normalized to .txt)."""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urljoin

import httpx
import trafilatura

if TYPE_CHECKING:
    from app.config import Settings

logger = logging.getLogger(__name__)

MAX_URL_LENGTH = 2048


class UrlImportError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _punycode_hostname(host: str) -> str:
    if not host or host.startswith("["):
        return host
    try:
        return host.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        raise UrlImportError(400, "Invalid hostname") from None


def _ip_from_sockaddr(sockaddr: object) -> str | None:
    if isinstance(sockaddr, tuple) and len(sockaddr) >= 1:
        addr = sockaddr[0]
        if isinstance(addr, str):
            return addr
    return None


def raise_for_private_or_meta_hosts(hostname: str) -> None:
    """Resolve hostname and reject loopback, private, link-local, and multicast targets (SSRF mitigation)."""
    host = _punycode_hostname(hostname.strip())
    if not host:
        raise UrlImportError(400, "Missing host")

    if host.startswith("["):
        inner = host.strip("[]")
        try:
            ip = ipaddress.ip_address(inner)
        except ValueError as e:
            raise UrlImportError(400, "Invalid host") from e
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
        ):
            raise UrlImportError(403, "URL resolves to a disallowed address")
        return

    try:
        ip = ipaddress.ip_address(host)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
        ):
            raise UrlImportError(403, "URL resolves to a disallowed address")
        return
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise UrlImportError(400, f"Could not resolve host: {e}") from e

    for info in infos:
        ip_s = _ip_from_sockaddr(info[4])
        if not ip_s:
            continue
        try:
            ip = ipaddress.ip_address(ip_s)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
        ):
            raise UrlImportError(403, "URL resolves to a disallowed address")


def _parse_and_validate_url(url: str) -> tuple[str, str, int | None]:
    raw = url.strip()
    if not raw:
        raise UrlImportError(400, "URL is required")
    if len(raw) > MAX_URL_LENGTH:
        raise UrlImportError(400, "URL is too long")

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise UrlImportError(400, "Only http and https URLs are allowed")

    host = parsed.hostname
    if not host:
        raise UrlImportError(400, "URL must include a host")

    port = parsed.port
    return raw, host, port


def _main_type(content_type: str | None) -> str:
    if not content_type:
        return ""
    return content_type.split(";")[0].strip().lower()


def _classify_body(content_type: str | None, body: bytes) -> str:
    if body.startswith(b"%PDF-"):
        return "pdf"
    mt = _main_type(content_type)
    if mt in ("application/pdf", "application/x-pdf") or "pdf" in mt:
        return "pdf"
    if mt in ("text/html", "application/xhtml+xml"):
        return "html"
    if mt in ("text/plain", "text/markdown", "text/x-markdown") or "markdown" in mt:
        return "text"
    if mt == "application/octet-stream":
        if body.startswith(b"%PDF-"):
            return "pdf"
        return "unknown_binary"
    return "unknown_binary"


def _html_to_plain_text(html: bytes) -> bytes:
    try:
        decoded = html.decode("utf-8")
    except UnicodeDecodeError:
        decoded = html.decode("utf-8", errors="replace")
    extracted = trafilatura.extract(decoded, output_format="txt")
    text = (extracted or "").strip()
    if not text:
        text = trafilatura.extract(decoded, output_format="txt", favor_recall=True) or ""
        text = text.strip()
    return text.encode("utf-8")


def _normalize_text_body(body: bytes) -> bytes:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = body.decode("utf-8", errors="replace")
    return text.encode("utf-8")


def _body_for_storage(kind: str, body: bytes, content_type: str | None) -> tuple[bytes, str]:
    if kind == "pdf":
        return body, ".pdf"
    if kind == "html":
        return _html_to_plain_text(body), ".txt"
    if kind == "text":
        mt = _main_type(content_type)
        ext = (
            ".md"
            if mt in ("text/markdown", "text/x-markdown") or "markdown" in mt
            else ".txt"
        )
        return _normalize_text_body(body), ext
    raise UrlImportError(
        415,
        "Unsupported content type; use http(s) URLs that serve HTML, PDF, plain text, or markdown.",
    )


async def fetch_url_document(url: str, settings: Settings) -> tuple[bytes, str]:
    """
    Download url (with redirect validation per hop), return (s3_body_bytes, extension_with_dot).
    """
    if not settings.url_import_enabled:
        raise UrlImportError(403, "URL import is disabled")

    current = url.strip()
    limits = httpx.Limits(max_keepalive_connections=5, max_connections=5)
    timeout = httpx.Timeout(settings.url_import_timeout_seconds)

    async with httpx.AsyncClient(
        limits=limits,
        timeout=timeout,
        follow_redirects=False,
        headers={"User-Agent": "document-processing-ingestion/1.0"},
    ) as client:
        for _ in range(settings.url_import_max_redirects + 1):
            raw, host, _port = _parse_and_validate_url(current)
            raise_for_private_or_meta_hosts(host)

            ct: str | None
            body: bytes
            try:
                async with client.stream("GET", raw) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        await response.aread()
                        loc = response.headers.get("location")
                        if not loc:
                            raise UrlImportError(502, "Redirect without Location header")
                        current = urljoin(raw, loc)
                        continue

                    response.raise_for_status()
                    ct = response.headers.get("content-type")
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > settings.url_import_max_bytes:
                            raise UrlImportError(413, "Response body exceeds configured limit")
                        chunks.append(chunk)
                    body = b"".join(chunks)
            except httpx.HTTPStatusError as e:
                raise UrlImportError(
                    502, f"Remote server returned {e.response.status_code}"
                ) from e
            except httpx.RequestError as e:
                raise UrlImportError(502, f"Could not fetch URL: {e}") from e

            kind = _classify_body(ct, body)
            try:
                return _body_for_storage(kind, body, ct)
            except UrlImportError:
                raise
            except Exception as e:
                logger.warning("url_import_normalize_failed", exc_info=True)
                raise UrlImportError(500, "Failed to process response body") from e

    raise UrlImportError(502, "Too many redirects")
