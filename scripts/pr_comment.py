#!/usr/bin/env python3
"""
Post (or update in place) the single build report comment on a pull request.

    pr_comment.py --repository owner/repo --pr 12 --bundle out/build.json --image-path ghcr.io/org/untrusted-pr/x \
        --primary-tag pr-12-abc1234 --pushed true --run-url URL --artifact-name bundle

The comment is found again by its hidden marker. ``compose`` is pure and unit-tested; GitHub calls go
through ``gh api``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

MARKER = "<!-- lsio-build-report -->"
WARNING = (
    "> [!WARNING]\n"
    "> This image is an **unreviewed pull request build**, published under the `untrusted-pr` namespace. "
    "It is not for production use, never receives a `latest` tag and is deleted when the pull request closes."
)


def compose(bundle: dict[str, Any], image_path: str, primary_tag: str, pushed: bool, run_url: str, artifact_name: str, platforms: list[str] | None = None) -> str:
    v = bundle.get("version", {})
    test = bundle.get("test", {})
    per_arch = test.get("per_arch") or {}
    plats = platforms or [p["arch"] for p in bundle.get("platforms", [])]
    status = "passed" if test.get("success") else ("disabled" if not test.get("enabled", True) else "failed")
    lines = [
        MARKER,
        f"### Build report for `{bundle.get('image')}` ({primary_tag})",
        "",
        f"Commit `{bundle.get('commit', '')[:7]}` · upstream `{v.get('upstream', '')}` · tests **{status}** · [workflow run]({run_url})",
        "",
        "| Arch | Digest | Compressed | Layers | Tests |",
        "| --- | --- | --- | --- | --- |",
    ]
    by_arch = {p["arch"]: p for p in bundle.get("platforms", [])}
    for arch in plats:
        p = by_arch.get(arch, {})
        digest = p.get("digest", "")
        size = p.get("size")
        size_s = f"{size / 1024 / 1024:.1f} MiB" if isinstance(size, int) and size else "n/a"
        t = per_arch.get(arch)
        t_s = "pass" if t is True else ("fail" if t is False else "n/a")
        lines.append(f"| {arch} | `{digest[:19]}` | {size_s} | {p.get('layers', 'n/a')} | {t_s} |")
    pk = bundle.get("packages") or {}
    if pk:
        lines.append("")
        lines.append("Packages: " + ", ".join(f"`{a}` {s['count']} ({s['distro']})" for a, s in pk.items()))
    lines.append("")
    if pushed:
        lines.append("Pull the quarantined image:")
        lines.append("")
        lines.append("```")
        lines.append(f"docker pull {image_path}:{primary_tag}")
        lines.append("```")
        lines.append("")
        lines.append(WARNING)
    else:
        lines.append(f"This pull request comes from a fork, so nothing was pushed. The image is attached to the workflow run as the `image-<arch>` artifact, and the bundle as `{artifact_name}`.")
    lines.append("")
    lines.append(f"Bundle (`build.json`, test reports, SBOMs) is the `{artifact_name}` artifact of the run.")
    return "\n".join(lines) + "\n"


def gh_api(args: list[str], body: dict[str, Any] | None = None) -> Any:
    cmd = ["gh", "api", *args]
    inp = None
    if body is not None:
        cmd += ["--input", "-"]
        inp = json.dumps(body)
    proc = subprocess.run(cmd, input=inp, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def upsert(repository: str, pr: int, body: str) -> str:
    comments = gh_api(["--paginate", f"repos/{repository}/issues/{pr}/comments?per_page=100"]) or []
    existing = next((c for c in comments if MARKER in (c.get("body") or "")), None)
    if existing:
        gh_api(["--method", "PATCH", f"repos/{repository}/issues/comments/{existing['id']}"], {"body": body})
        return "updated"
    gh_api(["--method", "POST", f"repos/{repository}/issues/{pr}/comments"], {"body": body})
    return "created"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repository", required=True)
    ap.add_argument("--pr", required=True, type=int)
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--image-path", required=True)
    ap.add_argument("--primary-tag", required=True)
    ap.add_argument("--pushed", default="true")
    ap.add_argument("--run-url", required=True)
    ap.add_argument("--artifact-name", default="bundle")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8")) if Path(args.bundle).is_file() else {"image": "unknown", "version": {}, "test": {}, "platforms": []}
    body = compose(bundle, args.image_path, args.primary_tag, args.pushed.lower() == "true", args.run_url, args.artifact_name)
    if args.dry_run:
        sys.stdout.write(body)
        return 0
    try:
        print(f"comment {upsert(args.repository, args.pr, body)}")
    except RuntimeError as exc:
        print(f"::warning::could not post the PR comment (forks have a read-only token): {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
