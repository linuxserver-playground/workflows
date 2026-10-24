#!/usr/bin/env python3
"""
Read image.yml and turn it into step outputs for the rest of the workflow.

Also decides the build mode (CONVENTIONS §2/§4):
  pr    the event is pull_request
  live  the pushed branch equals release.branch
  dev   anything else
and whether images may be pushed (not for pull requests from forks, which run without secrets).

Usage: manifest.py --manifest image.yml --event-name push --ref-name master --repository owner/docker-x \
          --sha <40 hex> [--pr-number N] [--fork true|false] [--work-dir DIR]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

import gha  # noqa: E402
import resolve_version  # noqa: E402

RUNNERS = {"amd64": "ubuntu-24.04", "arm64": "ubuntu-24.04-arm"}
DEFAULT_DOCKERFILES = {"amd64": "Dockerfile", "arm64": "Dockerfile.aarch64"}
ARCH_PREFIX = {"amd64": "amd64", "arm64": "arm64v8"}


def detect_mode(event_name: str, ref_name: str, release_branch: str) -> str:
    if event_name in {"pull_request", "pull_request_target"}:
        return "pr"
    if ref_name == release_branch:
        return "live"
    return "dev"


def first_sentence(summary: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", summary)  # [text](url) -> text
    text = re.sub(r"\s+", " ", text).strip()
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    sentence = m.group(1) if m else text
    return sentence.replace('"', "'")[:500]


def base_image_from(dockerfile: Path) -> str:
    if not dockerfile.is_file():
        return ""
    for line in dockerfile.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip().upper().startswith("FROM "):
            parts = line.strip().split()
            ref = parts[1]
            # FROM --platform=... ref
            for p in parts[1:]:
                if not p.startswith("--"):
                    ref = p
                    break
            return ref
    return ""


def compute(manifest: dict[str, Any], *, event_name: str, ref_name: str, repository: str, sha: str, pr_number: str, fork: bool, repo_dir: Path) -> dict[str, Any]:
    name = manifest["name"]
    release = manifest["release"]
    build = manifest["build"]
    platforms = list(build["platforms"])
    mode = detect_mode(event_name, ref_name, release["branch"])
    owner = repository.split("/")[0]
    namespace = {"live": f"ghcr.io/{owner}/{name}", "dev": f"ghcr.io/{owner}/dev/{name}", "pr": f"ghcr.io/{owner}/untrusted-pr/{name}"}[mode]
    dockerfiles = {**DEFAULT_DOCKERFILES, **(build.get("dockerfiles") or {})}
    matrix = {
        "include": [
            {"arch": arch, "runner": RUNNERS[arch], "dockerfile": dockerfiles[arch], "arch_prefix": ARCH_PREFIX[arch]}
            for arch in platforms
        ]
    }
    test = dict(manifest.get("test") or {})
    test.setdefault("enabled", True)
    summary = manifest.get("summary", "")
    if mode == "live":
        description = first_sentence(summary)
    else:
        description = f"UNREVIEWED {mode} build of {name}. Not for production use."
    other_tags = [t["tag"] for t in (release.get("tags") or []) if t.get("tag") != release["tag"]]
    deprecated = bool((manifest.get("deprecation") or {}).get("status"))
    return {
        "name": name,
        "title": manifest.get("title") or name.capitalize(),
        "repo_name": f"docker-{name}",
        "base": build["base"],
        "base_image": base_image_from(repo_dir / dockerfiles["amd64"]),
        "platforms": platforms,
        "matrix": matrix,
        "dockerfiles": dockerfiles,
        "version_arg": build.get("version_arg", ""),
        "build_args": build.get("build_args") or {},
        "first_party": build.get("first_party", True),
        "release_tag": release["tag"],
        "release_branch": release["branch"],
        "release_channel": release["channel"],
        "other_tags": ",".join(other_tags),
        "test": test,
        "test_enabled": test["enabled"],
        "mode": mode,
        "push_images": not fork,
        "image_path": namespace,
        "description": description,
        "deprecated": deprecated,
        "manifest_json": json.dumps(manifest, separators=(",", ":"), ensure_ascii=False),
        "branch": ref_name,
        "sha": sha,
        "sha7": sha[:7],
        "pr_number": pr_number,
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default="image.yml")
    ap.add_argument("--event-name", required=True)
    ap.add_argument("--ref-name", required=True, help="branch name (for PRs: the base branch)")
    ap.add_argument("--repository", required=True)
    ap.add_argument("--sha", required=True)
    ap.add_argument("--pr-number", default="")
    ap.add_argument("--fork", default="false")
    ap.add_argument("--work-dir", default="", help="where manifest.json is written")
    args = ap.parse_args(argv)
    path = Path(args.manifest)
    manifest = resolve_version.load_manifest(path)
    out = compute(
        manifest,
        event_name=args.event_name,
        ref_name=args.ref_name,
        repository=args.repository,
        sha=args.sha,
        pr_number=args.pr_number,
        fork=args.fork.lower() == "true",
        repo_dir=path.resolve().parent,
    )
    if args.work_dir:
        work = Path(args.work_dir)
        work.mkdir(parents=True, exist_ok=True)
        (work / "manifest.json").write_text(out["manifest_json"] + "\n", encoding="utf-8")
        out["manifest_path"] = str(work / "manifest.json")
    gha.set_outputs(out)
    gha.summary(f"**{out['title']}** (`{out['name']}`) mode `{out['mode']}`, platforms `{' '.join(out['platforms'])}`, image `{out['image_path']}`")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
