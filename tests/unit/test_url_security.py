import pytest

from app.core.exceptions import ValidationError
from app.core.url_security import normalize_crawl_url


def test_normalize_rejects_non_http_protocol():
    with pytest.raises(ValidationError):
        normalize_crawl_url("file:///etc/passwd")


def test_normalize_rejects_url_credentials():
    with pytest.raises(ValidationError):
        normalize_crawl_url("https://user:pass@example.com/post")


def test_normalize_rejects_private_ip_literal():
    with pytest.raises(ValidationError):
        normalize_crawl_url("http://127.0.0.1/admin")


def test_normalize_rejects_invalid_port():
    with pytest.raises(ValidationError):
        normalize_crawl_url("https://example.com:bad/path")


def test_normalize_removes_fragment_and_lowercases_host():
    assert (
        normalize_crawl_url("HTTPS://Example.COM/a?b=1#frag")
        == "https://example.com/a?b=1"
    )
