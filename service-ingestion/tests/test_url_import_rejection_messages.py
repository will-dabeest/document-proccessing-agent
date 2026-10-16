"""User-visible URL rejection reasons, including the pre-DNS missing-host guard."""

from unittest.mock import patch

import pytest

from app.url_import import UrlImportError, _parse_and_validate_url, raise_for_private_or_meta_hosts


@pytest.mark.parametrize("url", ["", "   ", "\n\t"])
def test_blank_url_detail_is_required(url):
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url(url)
    assert exc.value.status_code == 400
    assert exc.value.detail == "URL is required"


@pytest.mark.parametrize("host", ["", "   ", "\n\t"])
def test_missing_host_is_rejected_before_dns(host):
    with patch("app.url_import.socket.getaddrinfo") as resolve:
        with pytest.raises(UrlImportError) as exc:
            raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 400
    assert exc.value.detail == "Missing host"
    resolve.assert_not_called()
