#!/usr/bin/env python3
"""
Work out the LinuxServer build number (N in ``-lsN``) for the next release of a branch.

CONVENTIONS §3: N starts at 1, increments on every release of the branch and is read from the
branch's latest GitHub Release tag, never from a registry. The tag shape is
``[T-]<upstream>-ls<N>`` where T is the branch's release tag (absent when T is ``latest``).

    python3 next_tag.py --repo owner/docker-foo --branch master --release-tag latest \
        [--other-tags develop,nightly] [--head-tags tag1,tag2] [--fresh]

Prints JSON: {"ls_build": N, "previous_tag": "...", "previous_ls_build": M,
"previous_release_url": "...", "head_already_released": bool, "diff_base": "..."}. `diff_base` is the newest
release of the branch not made from this commit: what the build's record.json diffs against.

Releases are read with ``gh api --paginate`` (retried on network failures, scripts/netretry.py); ``pick`` is pure and unit-tested with fixtures.

On the branch that publishes ``latest`` (unprefixed tags), the other live branches' tags keep their releases out:
those named in ``--other-tags`` (the manifest's ``release.tags``) and those of every other branch whose own image.yml
names it in ``release.branch``. A branch is live before the README advertises it, so ``release.tags`` alone would let
master claim ``romm-1.0-ls1``.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.parse
from typing import Any

import netretry


def list_releases(repo: str) -> list[dict[str, Any]]:
    proc = netretry.run(["gh", "api", "--paginate", "--slurp", f"repos/{repo}/releases?per_page=100"])
    if proc.returncode != 0:
        raise RuntimeError(f"gh api releases failed: {proc.stderr.strip()}")
    pages = json.loads(proc.stdout or "[]")
    releases: list[dict[str, Any]] = []
    for page in pages:
        releases.extend(page)
    return releases


def gh(path: str, *args: str) -> str | None:
    proc = netretry.run(["gh", "api", *args, path])
    return proc.stdout if proc.returncode == 0 else None


def live_sibling_tags(repo: str, branch: str) -> list[str]:
    """Tags of the repo's other live branches: every branch whose own image.yml names it in release.branch."""
    try:
        import yaml  # noqa: PLC0415 - installed with the builder by the manifest step; never fail a build on it
    except ImportError:
        print("warning: PyYAML missing; only release.tags keeps other branches' releases out", file=sys.stderr)
        return []
    listing = gh(f"repos/{repo}/branches?per_page=100", "--paginate", "--jq", ".[].name")
    if listing is None:
        print("warning: could not list branches; only release.tags keeps other branches' releases out", file=sys.stderr)
        return []
    tags: list[str] = []
    for name in listing.split():
        if name == branch:
            continue
        ref = urllib.parse.quote(name, safe="")
        text = gh(f"repos/{repo}/contents/image.yml?ref={ref}", "-H", "Accept: application/vnd.github.raw")
        try:
            release = (yaml.safe_load(text) or {}).get("release") or {} if text else {}
        except yaml.YAMLError:
            continue
        if isinstance(release, dict) and release.get("branch") == name and release.get("tag"):
            tags.append(str(release["tag"]))
    return tags


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
    found: list[tuple[str, int, str]] = []
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
        found.append((tag, int(m.group("n")), rel.get("html_url") or ""))
        # The previous release decides the build number; the diff needs the newest one that is not this commit
        # (a rebuild of a released head republishes its tag), so keep looking once past a head tag.
        if tag not in head_tags:
            break
    if not found:
        return {
            "ls_build": 1,
            "previous_tag": "",
            "previous_ls_build": 0,
            "previous_release_url": "",
            "head_already_released": False,
            "diff_base": "",
        }
    tag, previous_n, url = found[0]
    return {
        "ls_build": previous_n if tag in head_tags else previous_n + 1,
        "previous_tag": tag,
        "previous_ls_build": previous_n,
        "previous_release_url": url,
        "head_already_released": tag in head_tags,
        "diff_base": next((t for t, _, _ in found if t not in head_tags), ""),
    }


BOT_SUBJECTS = ("Update package versions", "Update generated files")
"""The pipeline's own commits (CONVENTIONS §8). Neither changes what the image is built from: the package table is
written after a release, the generated files before the build of the run that made them. A head reached from a
released commit through nothing but these is that released commit for the build number."""


def own_commit(subject: str) -> bool:
    return subject.startswith(BOT_SUBJECTS) and "[skip ci]" in subject


def head_tags_from_git(limit: int = 50) -> list[str]:
    """Tags on HEAD, and on the ancestors HEAD reaches through the pipeline's own `[skip ci]` commits only."""
    proc = subprocess.run(
        ["git", "log", f"-n{limit}", "--format=%H%x1f%s", "HEAD"], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        return []
    tags: list[str] = []
    for line in proc.stdout.splitlines():
        sha, _, subject = line.partition("\x1f")
        at = subprocess.run(["git", "tag", "--points-at", sha], capture_output=True, text=True, check=False)
        if at.returncode == 0:
            tags += [t for t in at.stdout.split() if t and t not in tags]
        if not own_commit(subject):
            break
    return tags


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="owner/repo")
    ap.add_argument("--branch", required=True)
    ap.add_argument("--release-tag", required=True)
    ap.add_argument("--other-tags", default="", help="comma separated tags of the other live branches")
    ap.add_argument("--head-tags", default=None, help="comma separated tags pointing at HEAD (default: git)")
    ap.add_argument(
        "--fresh",
        action="store_true",
        help="a new build number whatever points at HEAD (a package check that found changes never republishes)",
    )
    ap.add_argument("--releases-json", default=None, help="read releases from this file instead of gh (tests)")
    ap.add_argument("--no-siblings", action="store_true", help="do not read the other branches' image.yml (tests)")
    args = ap.parse_args(argv)

    if args.releases_json:
        with open(args.releases_json, encoding="utf-8") as fh:
            releases = json.load(fh)
    else:
        releases = list_releases(args.repo)
    head_tags = [] if args.fresh else (args.head_tags.split(",") if args.head_tags is not None else head_tags_from_git())
    other = [t.strip() for t in args.other_tags.split(",") if t.strip()]
    if args.release_tag == "latest" and not args.no_siblings:
        other += [t for t in live_sibling_tags(args.repo, args.branch) if t not in other]
    print(json.dumps(pick(releases, args.release_tag, args.branch, other, head_tags)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
