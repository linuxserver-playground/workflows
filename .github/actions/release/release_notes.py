#!/usr/bin/env python3
"""
Release body assembly (CONVENTIONS §7) and the final build.json patch before upload.

    release_notes.py compose --ci-url URL --linuxserver-notes FILE --upstream-notes FILE --upstream-url URL
    release_notes.py patch-bundle build.json --notes notes.json --asset-url-base URL

The body has three sections in order: CI, LinuxServer changes, Upstream changes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

MAX_UPSTREAM_CHARS = 60_000  # GitHub caps release bodies at 125k; keep room for the other sections.


def clean_generated_notes(text: str) -> str:
    text = re.sub(r"^## What.s Changed\s*", "", text.strip(), flags=re.MULTILINE)
    return text.strip() or "No changes"


def ci_section(report_url: str, status_url: str) -> str:
    """Hub page with the rendered results first, control plane live status second (CONVENTIONS §10)."""
    links = []
    if report_url:
        links.append(f"[Test report]({report_url})")
    if status_url:
        links.append(f"[Build status]({status_url})")
    return " · ".join(links) if links else "No test report."


def compose(ci_url: str, linuxserver_notes: str, upstream_notes: str, upstream_url: str, status_url: str = "") -> str:
    upstream = upstream_notes.strip()
    if len(upstream) > MAX_UPSTREAM_CHARS:
        upstream = upstream[:MAX_UPSTREAM_CHARS].rstrip() + "\n\n_(truncated)_"
    parts = [
        "## CI",
        "",
        ci_section(ci_url, status_url),
        "",
        "## LinuxServer changes",
        "",
        clean_generated_notes(linuxserver_notes),
        "",
        "## Upstream changes",
        "",
    ]
    if upstream_url:
        parts.append(f"Source: {upstream_url}")
        parts.append("")
    parts.append(upstream or "No upstream notes.")
    parts.append("")
    return "\n".join(parts)


def patch_bundle(bundle: dict[str, Any], notes: dict[str, str], asset_url_base: str, release_url: str = "") -> dict[str, Any]:
    bundle = dict(bundle)
    note_block = {k: v for k, v in {"linuxserver": notes.get("linuxserver", ""), "upstream": notes.get("upstream", ""), "upstream_url": notes.get("upstream_url", "")}.items() if v}
    if note_block:
        bundle["notes"] = note_block
    if asset_url_base:
        base = asset_url_base.rstrip("/")
        bundle["assets"] = [{**a, "url": f"{base}/{a['name']}"} for a in bundle.get("assets", [])]
    if release_url and bundle.get("test") is not None:
        pass  # report_url stays what emit-bundle set; the release URL is carried by the control event
    return bundle


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compose")
    c.add_argument("--ci-url", default="")
    c.add_argument("--status-url", default="")
    c.add_argument("--linuxserver-notes", required=True)
    c.add_argument("--upstream-notes", required=True, help="JSON file {text, url} from upstream_notes.py")
    c.add_argument("--notes-out", default="", help="write {linuxserver, upstream, upstream_url} JSON here")
    p = sub.add_parser("patch-bundle")
    p.add_argument("bundle")
    p.add_argument("--notes", required=True)
    p.add_argument("--asset-url-base", required=True)
    args = ap.parse_args(argv)

    if args.cmd == "compose":
        ls = Path(args.linuxserver_notes).read_text(encoding="utf-8")
        up = json.loads(Path(args.upstream_notes).read_text(encoding="utf-8"))
        body = compose(args.ci_url, ls, up.get("text", ""), up.get("url", ""), status_url=args.status_url)
        if args.notes_out:
            Path(args.notes_out).write_text(json.dumps({"linuxserver": clean_generated_notes(ls), "upstream": up.get("text", ""), "upstream_url": up.get("url", "")}), encoding="utf-8")
        sys.stdout.write(body)
        return 0
    bundle_path = Path(args.bundle)
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    notes = json.loads(Path(args.notes).read_text(encoding="utf-8"))
    bundle_path.write_text(json.dumps(patch_bundle(bundle, notes, args.asset_url_base), indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
