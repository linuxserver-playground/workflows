#!/usr/bin/env python3
"""
Delete every version of an organisation container package tagged for a pull request.

    pr_cleanup.py --org linuxserver-playground --package untrusted-pr/sonarr --pr 12 [--dry-run]

Matching tags: ``pr-<n>-*`` and its arch twins ``<prefix>-pr-<n>-*`` (image builds), ``*-pr-<n>`` (mod builds). Uses ``gh api`` with
GH_TOKEN. ``select_versions`` is pure and unit-tested.

Exit codes: 0 when nothing was left to delete, 2 when at least one version could not be deleted (the
caller may retry with a token that has delete:packages on the organisation). Listing failures are
warnings and exit 0: a missing package means there is nothing to clean.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from typing import Any

import netretry


def tag_matches(tag: str, pr: int) -> bool:
    """pr-<n>-<sha> and its arch twins <prefix>-pr-<n>-<sha> (image builds), *-pr-<n> (mod builds)."""
    return bool(re.match(rf"^(?:[a-z0-9]+-)?pr-{pr}-", tag)) or bool(re.search(rf"-pr-{pr}$", tag))


def select_versions(versions: list[dict[str, Any]], pr: int) -> list[dict[str, Any]]:
    chosen = []
    for v in versions:
        tags = ((v.get("metadata") or {}).get("container") or {}).get("tags") or []
        if any(tag_matches(t, pr) for t in tags):
            chosen.append(v)
    return chosen


def gh_api(args: list[str]) -> Any:
    proc = netretry.run(["gh", "api", *args])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", required=True)
    ap.add_argument("--package", required=True)
    ap.add_argument("--pr", required=True, type=int)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    pkg = urllib.parse.quote(args.package, safe="")
    base = f"orgs/{args.org}/packages/container/{pkg}"
    try:
        pages = gh_api(["--paginate", "--slurp", f"{base}/versions?per_page=100"]) or []
    except RuntimeError as exc:
        if "404" in exc.args[0] or "Not Found" in exc.args[0]:
            print(f"package {args.package} does not exist; nothing to delete")
            return 0
        print(f"::warning::could not list versions of {args.package}: {exc}")
        return 0
    versions = [v for page in pages for v in page]
    chosen = select_versions(versions, args.pr)
    if not chosen:
        print(f"no versions of {args.package} tagged for PR #{args.pr}")
        return 0
    failures = 0
    for v in chosen:
        tags = ",".join(v["metadata"]["container"]["tags"])
        if args.dry_run:
            print(f"would delete version {v['id']} ({tags})")
            continue
        try:
            gh_api(["--method", "DELETE", f"{base}/versions/{v['id']}"])
            print(f"deleted version {v['id']} ({tags})")
        except RuntimeError as exc:
            failures += 1
            print(f"::warning::could not delete version {v['id']} ({tags}): {exc}")
    if failures:
        print("::warning::some versions were not deleted; the token needs delete:packages on the organisation")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
