#!/usr/bin/env python3
"""
Compare the package table of a fresh image with the one committed to the repository.

This is the `reason: packages` check (Jenkins parity, stage "Update-packages"): the weekly run builds
and tests as usual, then compares ``package_versions.amd64.txt`` from the ci output with the committed
``package_versions.txt`` (the amd64 table) at the build sha. Content is compared by md5 after
normalising trailing whitespace, so a rewrite that only changes line endings is not a change. A missing
committed file counts as changed: the first run seeds it.

    package_versions.py compare --fresh out/package_versions.amd64.txt [--committed package_versions.txt] \
        [--previous-arm64-url URL --fresh-arm64 out/package_versions.arm64.txt]

Prints JSON: {"changed": bool, "status": "same"|"different"|"missing", "fresh": <md5-8>,
"committed": <md5-8>|"none", "arm64": {...}|null}. The arm64 block compares the fresh arm64 table with
the previous release's asset and is reporting only. Pure functions are unit-tested; the URL fetch is
the only I/O.
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
    cmp_.add_argument("--fresh-arm64", default="", help="package_versions.arm64.txt from the ci output")
    cmp_.add_argument("--previous-arm64-url", default="", help="previous release's package_versions.arm64.txt")
    args = ap.parse_args(argv)

    fresh = Path(args.fresh).read_text(encoding="utf-8", errors="replace")
    committed = Path(args.committed).read_text(encoding="utf-8", errors="replace") if args.committed and Path(args.committed).is_file() else None
    result = compare(fresh, committed)
    result["arm64"] = None
    if args.fresh_arm64 and Path(args.fresh_arm64).is_file() and args.previous_arm64_url:
        previous = fetch(args.previous_arm64_url)
        fresh_arm64 = Path(args.fresh_arm64).read_text(encoding="utf-8", errors="replace")
        result["arm64"] = compare(fresh_arm64, previous) if previous is not None else {"changed": None, "status": "unavailable", "fresh": digest(fresh_arm64), "committed": "none"}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
