#!/usr/bin/env python3
"""
The tag set for one build (CONVENTIONS §3 and §4).

    compute_tags(mode, release_tag, upstream_clean, semver, ls_build, branch, sha7, pr_number, platforms)

Returns the primary tag, every unprefixed tag, the arch-prefixed twins and the version label.
Pure; used by the prepare job and unit-tested.

``staging_refs`` names the intermediate references the arch jobs push to
(``ghcr.io/<owner>/build/<name>:<arch>-<sha7>-<run_id>``). They are mode-independent, never consumed
by anyone but ``publish-manifest`` in the same run, and deleted by ``cleanup-staging.yml``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

ARCH_PREFIX = {"amd64": "amd64", "arm64": "arm64v8"}
STAGING_NAMESPACE = "build"


def sanitize_component(value: str) -> str:
    """Make a branch name legal as a docker tag component."""
    value = re.sub(r"[^A-Za-z0-9_.-]", "-", value)
    value = value.lstrip(".-")
    return value[:120]


def compute_tags(
    mode: str,
    release_tag: str,
    upstream_clean: str,
    semver: str | None,
    ls_build: int,
    branch: str,
    sha7: str,
    pr_number: int | str | None,
    platforms: list[str],
) -> dict[str, Any]:
    if mode == "live":
        prefix = "" if release_tag == "latest" else f"{release_tag}-"
        primary = f"{prefix}{upstream_clean}-ls{ls_build}"
        tags = [release_tag, primary, f"{prefix}version-{upstream_clean}"]
        if semver:
            tags.append(f"{prefix}{semver}")
        version_label = f"{upstream_clean}-ls{ls_build}"
    elif mode == "dev":
        primary = f"{sanitize_component(branch)}-{sha7}"
        tags = [primary]
        version_label = primary
    elif mode == "pr":
        if pr_number in (None, ""):
            raise ValueError("pr mode needs a pull request number")
        primary = f"pr-{pr_number}-{sha7}"
        tags = [primary]
        version_label = primary
    else:
        raise ValueError(f"unknown mode {mode!r}")
    # Dedupe while keeping order (release_tag could equal something else only in odd manifests).
    seen: set[str] = set()
    tags = [t for t in tags if not (t in seen or seen.add(t))]
    arch_tags = {arch: [f"{ARCH_PREFIX[arch]}-{t}" for t in tags] for arch in platforms}
    all_tags = tags + [t for arch in platforms for t in arch_tags[arch]]
    return {
        "primary_tag": primary,
        "tags": tags,
        "arch_tags": arch_tags,
        "arch_primary": {arch: f"{ARCH_PREFIX[arch]}-{primary}" for arch in platforms},
        "all_tags": all_tags,
        "version_label": version_label,
    }


def staging_path(owner: str, name: str) -> str:
    return f"ghcr.io/{owner}/{STAGING_NAMESPACE}/{name}"


def staging_refs(owner: str, name: str, sha7: str, run_id: int | str, platforms: list[str]) -> dict[str, str]:
    """One staging reference per platform: <staging path>:<arch>-<sha7>-<run_id>."""
    if not owner or not name or not sha7 or str(run_id) in ("", "0"):
        raise ValueError("staging refs need owner, name, sha7 and a run id")
    base = staging_path(owner, name)
    return {arch: f"{base}:{arch}-{sha7}-{run_id}" for arch in platforms}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True, choices=["live", "dev", "pr"])
    ap.add_argument("--release-tag", required=True)
    ap.add_argument("--upstream-clean", default="")
    ap.add_argument("--semver", default="")
    ap.add_argument("--ls-build", type=int, default=0)
    ap.add_argument("--branch", default="")
    ap.add_argument("--sha", required=True)
    ap.add_argument("--pr-number", default="")
    ap.add_argument("--platforms", required=True, help="JSON array")
    ap.add_argument("--repository", default="", help="owner/repo; with --name and --run-id adds the staging refs")
    ap.add_argument("--name", default="", help="image name, for the staging refs")
    ap.add_argument("--run-id", default="", help="workflow run id, for the staging refs")
    args = ap.parse_args(argv)
    out = compute_tags(
        args.mode,
        args.release_tag,
        args.upstream_clean,
        args.semver or None,
        args.ls_build,
        args.branch,
        args.sha[:7],
        args.pr_number or None,
        json.loads(args.platforms),
    )
    if args.repository and args.name and args.run_id:
        owner = args.repository.split("/")[0]
        out["staging_path"] = staging_path(owner, args.name)
        out["staging_refs"] = staging_refs(owner, args.name, args.sha[:7], args.run_id, json.loads(args.platforms))
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
