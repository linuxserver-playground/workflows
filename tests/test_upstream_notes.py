"""upstream_notes: the rss source picks the feed item titled after the version and keeps its HTML verbatim."""

from __future__ import annotations

import resolve_version as rv
import upstream_notes as un

FEED = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>calibre changelog</title>
<item><title>calibre 9.17</title><link>https://calibre-ebook.com/download</link>
<description>&lt;div&gt;&lt;h2&gt;New Features&lt;/h2&gt;&lt;ul&gt;&lt;li&gt;JPEG XL&lt;/li&gt;&lt;/ul&gt;&lt;/div&gt;</description></item>
<item><title>calibre 9.16</title><link>https://calibre-ebook.com/download</link><description>older</description></item>
</channel></rss>"""

URL = "https://calibre-ebook.com/changelog.rss"


def _install(monkeypatch, status: int = 200, body: str = FEED) -> None:
    monkeypatch.setattr(rv, "http_get", lambda url, headers=None, **_: rv.HttpResponse(status, body.encode(), {}, url))


def _manifest() -> dict:
    return {"upstream": {"notes": {"type": "rss", "url": URL, "title": "calibre ${VERSION}"}}}


def test_rss_matches_the_version_without_v_and_trailing_zero(monkeypatch) -> None:
    _install(monkeypatch)
    notes = un.fetch_notes(_manifest(), "v9.17.0")
    assert notes == {"text": "<div><h2>New Features</h2><ul><li>JPEG XL</li></ul></div>", "url": "https://calibre-ebook.com/download"}


def test_rss_without_a_matching_item_is_empty(monkeypatch) -> None:
    _install(monkeypatch)
    assert un.fetch_notes(_manifest(), "9.18.0") == {"text": "", "url": URL}


def test_rss_unreachable_or_not_xml_is_empty(monkeypatch) -> None:
    _install(monkeypatch, status=503)
    assert un.fetch_notes(_manifest(), "9.17.0")["text"] == ""
    _install(monkeypatch, body="<html>")
    assert un.fetch_notes(_manifest(), "9.17.0")["text"] == ""


def test_version_spellings() -> None:
    assert un._version_spellings("v9.17.0") == ["v9.17.0", "v9.17", "9.17.0", "9.17"]
    assert un._version_spellings("10.0") == ["10.0"]
    assert un._version_spellings("2.0.0") == ["2.0.0", "2.0"]
