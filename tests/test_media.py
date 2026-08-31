"""Media helper tests (pure logic; no network)."""

import pytest

from botschat.media import EXT_BY_MIME, guess_content_type, resolve_url


@pytest.mark.parametrize(
    "base,url,expected",
    [
        ("http://127.0.0.1:8787", "/api/media/u_1/x.png", "http://127.0.0.1:8787/api/media/u_1/x.png"),
        ("http://127.0.0.1:8787/", "/api/media/u_1/x.png", "http://127.0.0.1:8787/api/media/u_1/x.png"),
        ("http://host:8787", "https://cdn.example.com/a.png", "https://cdn.example.com/a.png"),
    ],
)
def test_resolve_url(base, url, expected):
    assert resolve_url(base, url) == expected


def test_guess_content_type():
    assert guess_content_type("photo.png") == "image/png"
    assert guess_content_type("report.pdf?x=1") == "application/pdf"
    assert guess_content_type("noext") == "application/octet-stream"


def test_ext_by_mime_covers_common_types():
    assert EXT_BY_MIME["image/png"] == "png"
    assert EXT_BY_MIME["video/mp4"] == "mp4"
    assert EXT_BY_MIME["application/pdf"] == "pdf"
