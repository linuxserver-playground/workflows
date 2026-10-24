#!/usr/bin/env python3
"""
Work out the LinuxServer build number (N in ``-lsN``) for the next release of a branch.

CONVENTIONS §3: N starts at 1, increments on every release of the branch and is read from the
branch's latest GitHub Release tag, never from a registry. The tag shape is
``[T-]<upstream>-ls<N>`` where T is the branch's release tag (absent when T is ``latest``).

    python3 next_tag.py --repo owner/docker-foo --branch master --release-tag latest \
        [--other-tags develop,nightly] [--head-sha <sha>] [--head-tags tag1,tag2]

Prints JSON: {"ls_build": N, "previous_tag": "...", "previous_ls_build": M,
"previous_release_url": "..."}.

Releases are read with ``gh api --paginate``; ``pick`` is pure and unit-tested with fixtures.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Any


def list_releases(repo: str) -> list[dict[str, Any]]:
    proc = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", f"repos/{repo}/releases?per_page=100"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh api releases failed: {proc.stderr.strip()}")
    pages = json.loads(proc.stdout or "[]")
    releases: list[dict[str, Any]] = []
    for page in pages:
        releases.extend(page)
    return releases


def tag_pattern(release_tag: str) -> re.Pattern[str]:
    if release_tag == "latest":
        return re.compile(r"^(?P<upstream>.+)-ls(?P<n>\d+)$")
    return re.compile(rf"^{re.escape(release_tag)}-(?P<upstream>.+)-ls(?P<n>\d+)$")


def matches_branch(tag: str, release_tag: str, other_tags: list[str]) -> re.Match[str] | None:
    """Does this release tag belong to the branch publishing ``release_tag``?"""
    m = tag_pattern(release_tag).match(tag)
    if not m:
        return None
    if release_tag == "latest":
        # The unprefixed branch must not claim 'develop-1.2.3-ls4'.
        for other in other_tags:
            if other and other != "latest" and tag.startswith(f"{other}-"):
                return None
    return m


def pick(
    releases: list[dict[str, Any]],
    release_tag: str,
    branch: str,
    other_tags: list[str] | None = None,
    head_tags: list[str] | None = None,
) -> dict[str, Any]:
    """
    Choose the previous release of this branch and the next build number.

    ``releases`` is the GitHub API list, newest first. Releases whose ``target_commitish`` names
    another branch are ignored; a sha target is accepted when the tag shape matches.
    """
    other_tags = other_tags or []
    head_tags = set(head_tags or [])
    for rel in releases:
        if rel.get("draft"):
            continue
        tag = rel.get("tag_name") or ""
        target = rel.get("target_commitish") or ""
        if target and not re.fullmatch(r"[0-9a-f]{40}", target) and target != branch:
            continue
        m = matches_branch(tag, release_tag, other_tags)
        if not m:
            continue
        previous_n = int(m.group("n"))
        next_n = previous_n if tag in head_tags else previous_n + 1
        return {
            "ls_build": next_n,
            "previous_tag": tag,
            "previous_ls_build": previous_n,
            "previous_release_url": rel.get("html_url") or "",
            "head_already_released": tag in head_tags,
        }
    return {"ls_build": 1, "previous_tag": "", "previous_ls_build": 0, "previous_release_url": "", "head_already_released": False}


def head_tags_from_git() -> list[str]:
    proc = subprocess.run(["git", "tag", "--points-at", "HEAD"], capture_output=True, text=True, check=False)
    return [t for t in proc.stdout.split() if t] if proc.returncode == 0 else []


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="owner/repo")
    ap.add_argument("--branch", required=True)
    ap.add_argument("--release-tag", required=True)
    ap.add_argument("--other-tags", default="", help="comma separated tags of the other live branches")
    ap.add_argument("--head-tags", default=None, help="comma separated tags pointing at HEAD (default: git)")
    ap.add_argument("--releases-json", default=None, help="read releases from this file instead of gh (tests)")
    args = ap.parse_args(argv)

    if args.releases_json:
        with open(args.releases_json, encoding="utf-8") as fh:
            releases = json.load(fh)
    else:
        releases = list_releases(args.repo)
    head_tags = args.head_tags.split(",") if args.head_tags is not None else head_tags_from_git()
    other = [t.strip() for t in args.other_tags.split(",") if t.strip()]
    print(json.dumps(pick(releases, args.release_tag, args.branch, other, head_tags)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
