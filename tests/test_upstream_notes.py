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


def test_transform_reshapes_the_version_before_the_tag_lookup(monkeypatch) -> None:
    seen: list[str] = []

    def fake_get(url, headers=None, **_):
        seen.append(url)
        return rv.HttpResponse(200, b'{"body": "fixed things", "html_url": "https://github.com/nginx/nginx/releases/tag/release-1.30.6"}', {}, url)

    monkeypatch.setattr(rv, "http_get", fake_get)
    manifest = {"upstream": {"notes": {"type": "github_release", "repo": "nginx/nginx", "tag": "release-${VERSION}", "transform": [{"regex": r"-r\d+$", "replace": ""}]}}}
    notes = un.fetch_notes(manifest, "1.30.6-r0")
    assert seen == ["https://api.github.com/repos/nginx/nginx/releases/tags/release-1.30.6"]
    assert notes == {"text": "fixed things", "url": "https://github.com/nginx/nginx/releases/tag/release-1.30.6"}


def test_transform_can_build_a_path_shaped_tag(monkeypatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(rv, "http_get", lambda url, headers=None, **_: (seen.append(url), rv.HttpResponse(404, b"", {}, url))[1])
    manifest = {"upstream": {"notes": {"type": "github_release", "repo": "BOINC/boinc", "transform": [{"regex": r"^(\d+\.\d+)\.(\d+).*$", "replace": r"client_release/\1/\1.\2"}]}}}
    notes = un.fetch_notes(manifest, "8.2.8+dfsg+202512070716~ubuntu24.04.1")
    assert seen == ["https://api.github.com/repos/BOINC/boinc/releases/tags/client_release%2F8.2%2F8.2.8"]
    assert notes == {"text": "", "url": "https://github.com/BOINC/boinc/releases/tag/client_release/8.2/8.2.8"}


PLEX = b'{"computer": {"Linux": {"version": "1.43.5", "items_added": "(Metadata) NFO support\\nUpdated Web", "items_fixed": "(DVR) Harden validation\\n\\n(Transcoder) Crash"}}}'


def test_json_notes_keep_every_line_and_select_by_version(monkeypatch) -> None:
    monkeypatch.setattr(rv, "http_get", lambda url, headers=None, **_: rv.HttpResponse(200, PLEX, {}, url))
    jq = '.computer.Linux | select(.version == "${VERSION}") | "### Added\\n\\(.items_added)\\n\\n### Fixed\\n\\(.items_fixed)"'
    manifest = {"upstream": {"notes": {"type": "json", "url": "https://plex.tv/api/downloads/5.json", "jq": jq}}}
    notes = un.fetch_notes(manifest, "1.43.5")
    assert notes["text"] == "### Added\n(Metadata) NFO support\nUpdated Web\n\n### Fixed\n(DVR) Harden validation\n\n(Transcoder) Crash"
    assert notes["url"] == "https://plex.tv/api/downloads/5.json"
    assert un.fetch_notes(manifest, "1.43.6") == {"text": "", "url": "https://plex.tv/api/downloads/5.json"}
