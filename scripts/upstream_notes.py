#!/usr/bin/env python3
"""
Fetch upstream release notes as described by the manifest's ``upstream.notes`` block.

    python3 upstream_notes.py <image.json|image.yml> <upstream version>

Prints JSON ``{"text": "...", "url": "..."}``. Text is verbatim (CONVENTIONS §7); an empty text
with type ``none`` is normal. Shares the HTTP layer with resolve_version so tests can mock it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import resolve_version as rv


def _tag_for(notes: dict[str, Any], version: str) -> str:
    template = notes.get("tag") or "${VERSION}"
    return template.replace("${VERSION}", version).replace("$VERSION", version)


def _github_release(notes: dict[str, Any], version: str) -> dict[str, str]:
    repo = notes["repo"]
    tag = _tag_for(notes, version)
    url = f"https://api.github.com/repos/{repo}/releases/tags/{urllib.parse.quote(tag, safe='')}"
    resp = rv.http_get(url, headers=rv.github_headers())
    if resp.status != 200:
        return {"text": "", "url": f"https://github.com/{repo}/releases/tag/{tag}"}
    data = resp.json()
    return {"text": data.get("body") or "", "url": data.get("html_url") or f"https://github.com/{repo}/releases/tag/{tag}"}


def _gitea_like(api_base: str, web_base: str, notes: dict[str, Any], version: str) -> dict[str, str]:
    repo = notes["repo"]
    tag = _tag_for(notes, version)
    resp = rv.http_get(f"{api_base}/repos/{repo}/releases/tags/{urllib.parse.quote(tag, safe='')}")
    web = f"{web_base}/{repo}/releases/tag/{tag}"
    if resp.status != 200:
        return {"text": "", "url": web}
    data = resp.json()
    return {"text": data.get("body") or "", "url": data.get("html_url") or web}


def _github_commit(notes: dict[str, Any], version: str) -> dict[str, str]:
    repo = notes["repo"]
    ref = version if version else notes.get("branch", "")
    resp = rv.http_get(f"https://api.github.com/repos/{repo}/commits/{urllib.parse.quote(ref, safe='')}", headers=rv.github_headers())
    if resp.status != 200:
        return {"text": "", "url": f"https://github.com/{repo}/commits/{notes.get('branch', '')}"}
    data = resp.json()
    return {"text": data.get("commit", {}).get("message") or "", "url": data.get("html_url") or ""}


def _gitlab_commit(notes: dict[str, Any], version: str) -> dict[str, str]:
    ref = version if version else notes.get("branch", "")
    url = f"https://gitlab.com/api/v4/projects/{notes['project_id']}/repository/commits/{urllib.parse.quote(ref, safe='')}"
    resp = rv.http_get(url)
    if resp.status != 200:
        return {"text": "", "url": url}
    data = resp.json()
    return {"text": data.get("message") or data.get("title") or "", "url": data.get("web_url") or url}


def _json(notes: dict[str, Any], version: str) -> dict[str, str]:
    url = notes["url"].replace("${VERSION}", version)
    headers = rv.github_headers() if "api.github.com" in url else None
    resp = rv.http_get(url, headers=headers)
    if resp.status != 200:
        return {"text": "", "url": url}
    return {"text": rv.run_jq(notes["jq"], resp.body), "url": url}


def _version_spellings(version: str) -> list[str]:
    """9.17.0 -> 9.17.0, 9.17; v1.2.0 -> v1.2.0, 1.2.0, v1.2, 1.2: feeds title releases the way the project says them."""
    out: list[str] = []
    for v in (version, version[1:] if version[:1] in ("v", "V") else version):
        while True:
            if v not in out:
                out.append(v)
            if not v.endswith(".0") or v.count(".") < 2:
                break
            v = v[:-2]
    return out


def _rss(notes: dict[str, Any], version: str) -> dict[str, str]:
    """The description of the feed item titled after the version: usually HTML, kept verbatim (the site sanitises)."""
    url = notes["url"].replace("${VERSION}", version)
    resp = rv.http_get(url)
    if resp.status != 200:
        return {"text": "", "url": url}
    try:
        root = ET.fromstring(resp.body)
    except ET.ParseError:
        return {"text": "", "url": url}
    wanted = {notes["title"].replace("${VERSION}", v).replace("$VERSION", v).strip() for v in _version_spellings(version)}
    for item in root.iter("item"):
        if (item.findtext("title") or "").strip() in wanted:
            link = (item.findtext("link") or "").strip()
            return {"text": (item.findtext("description") or "").strip(), "url": link if link.startswith("http") else url}
    return {"text": "", "url": url}


def _command(notes: dict[str, Any], version: str) -> dict[str, str]:
    env = {**os.environ, "VERSION": version}
    proc = subprocess.run(["bash", "-o", "pipefail", "-c", notes["run"]], capture_output=True, text=True, check=False, env=env)
    return {"text": proc.stdout if proc.returncode in (0, *rv.CLOSED_PIPE_EXITS) else "", "url": "command"}


def fetch_notes(manifest: dict[str, Any], version: str) -> dict[str, str]:
    notes = manifest.get("upstream", {}).get("notes") or {"type": "none"}
    kind = notes.get("type", "none")
    if kind == "none":
        return {"text": "", "url": ""}
    if kind == "github_release":
        return _github_release(notes, version)
    if kind == "codeberg_release":
        return _gitea_like("https://codeberg.org/api/v1", "https://codeberg.org", notes, version)
    if kind == "gitea_release":
        base = notes["base_url"].rstrip("/")
        return _gitea_like(f"{base}/api/v1", base, notes, version)
    if kind == "github_commit":
        return _github_commit(notes, version)
    if kind == "gitlab_commit":
        return _gitlab_commit(notes, version)
    if kind == "json":
        return _json(notes, version)
    if kind == "rss":
        return _rss(notes, version)
    if kind == "command":
        return _command(notes, version)
    raise rv.ResolveError(f"unknown notes type {kind!r}")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: upstream_notes.py <image.yml|image.json> <version>", file=sys.stderr)
        return 2
    manifest = rv.load_manifest(Path(argv[0]))
    try:
        with rv.retries():
            notes = fetch_notes(manifest, argv[1])
        print(json.dumps(notes))
    except rv.ResolveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
