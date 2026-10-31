#!/usr/bin/env python3
"""
Compare the package table of a fresh image with the one committed to the repository.

This is the `reason: packages` check (Jenkins parity, stage "Update-packages"): the weekly run builds
and tests as usual, then compares ``package_versions.amd64.txt`` from the ci output with the committed
``package_versions.txt`` (the amd64 table) at the build sha. Content is compared by md5 after
normalising trailing whitespace, so a rewrite that only changes line endings is not a change.

The committed table is the fast path, not the authority. When it differs or is missing, the fresh table is
compared with the previous release's ``package_versions.amd64.txt`` asset: identical means no package changed
since that release and only the branch's file is stale (it was written by another pipeline, or never), so the
run publishes nothing and commits the fresh table to catch the branch up (``catch_up``). Without that, every
branch whose table was not this pipeline's released once for nothing on its first check (craft-apps
v0.18.0-ls5, 2026-10-31: the same image as ls4, released because the file was still Jenkins' table). A
missing committed file with no previous release counts as changed: the first run seeds it.

    package_versions.py compare --fresh out/package_versions.amd64.txt [--committed package_versions.txt] \
        [--previous-amd64-url URL] [--previous-arm64-url URL --fresh-arm64 out/package_versions.arm64.txt]

Prints JSON: {"changed": bool, "status": "same"|"different"|"missing"|"released", "fresh": <md5-8>,
"committed": <md5-8>|"none", "catch_up": bool, "previous": {...}|null, "arm64": {...}|null}. ``released`` is
the catch-up case: the committed table differs or is missing, the previous release's is the same. ``previous``
is that comparison (``status`` ``unavailable`` when the asset could not be read, in which case the committed
table decides as before). The arm64 block compares the fresh arm64 table with the previous release's asset
and is reporting only. Pure functions are unit-tested; the URL fetch is the only I/O.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import netretry


def normalise(text: str) -> str:
    """Strip trailing whitespace from every line and trailing blank lines; one final newline."""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n" if lines else ""


def digest(text: str) -> str:
    """The first 8 hex characters of the md5 of the normalised table (the shape Jenkins used)."""
    return hashlib.md5(normalise(text).encode("utf-8")).hexdigest()[:8]  # noqa: S324 - identity, not security


def compare(fresh: str, committed: str | None) -> dict[str, Any]:
    fresh_digest = digest(fresh)
    if committed is None:
        return {"changed": True, "status": "missing", "fresh": fresh_digest, "committed": "none"}
    committed_digest = digest(committed)
    changed = committed_digest != fresh_digest
    return {"changed": changed, "status": "different" if changed else "same", "fresh": fresh_digest, "committed": committed_digest}


def decide(fresh: str, committed: str | None, previous: str | None) -> dict[str, Any]:
    """The compare result with the previous release's table consulted when the committed one does not match.

    previous is the previous release's amd64 table, None when there is no previous release or its asset
    could not be read: then the committed table decides on its own.
    """
    result = compare(fresh, committed)
    result["catch_up"] = False
    result["previous"] = None
    if not result["changed"] or previous is None:
        return result
    result["previous"] = compare(fresh, previous)
    if not result["previous"]["changed"]:
        result["changed"] = False
        result["status"] = "released"
        result["catch_up"] = True
    return result


def fetch(url: str, timeout: int = 30) -> str | None:
    """The text at url, retried on network failures; None when it is missing or cannot be read."""

    def get() -> str:
        req = urllib.request.Request(url, headers={"User-Agent": "linuxserver-playground-workflows/1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.read().decode("utf-8", errors="replace")

    try:
        return netretry.retry(get, what=f"GET {url}")
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        return None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    cmp_ = sub.add_parser("compare")
    cmp_.add_argument("--fresh", required=True, help="package_versions.amd64.txt from the ci output")
    cmp_.add_argument("--committed", default="", help="package_versions.txt at the build sha; absent means missing")
    cmp_.add_argument("--previous-amd64-url", default="", help="previous release's package_versions.amd64.txt; consulted when the committed table does not match")
    cmp_.add_argument("--fresh-arm64", default="", help="package_versions.arm64.txt from the ci output")
    cmp_.add_argument("--previous-arm64-url", default="", help="previous release's package_versions.arm64.txt")
    args = ap.parse_args(argv)

    fresh = Path(args.fresh).read_text(encoding="utf-8", errors="replace")
    committed = Path(args.committed).read_text(encoding="utf-8", errors="replace") if args.committed and Path(args.committed).is_file() else None
    previous = None
    if args.previous_amd64_url and compare(fresh, committed)["changed"]:
        previous = fetch(args.previous_amd64_url)
    result = decide(fresh, committed, previous)
    if args.previous_amd64_url and previous is None and result["changed"]:
        result["previous"] = {"changed": None, "status": "unavailable", "fresh": result["fresh"], "committed": "none"}
    result["arm64"] = None
    if args.fresh_arm64 and Path(args.fresh_arm64).is_file() and args.previous_arm64_url:
        previous = fetch(args.previous_arm64_url)
        fresh_arm64 = Path(args.fresh_arm64).read_text(encoding="utf-8", errors="replace")
        result["arm64"] = compare(fresh_arm64, previous) if previous is not None else {"changed": None, "status": "unavailable", "fresh": digest(fresh_arm64), "committed": "none"}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
