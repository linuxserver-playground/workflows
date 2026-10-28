#!/usr/bin/env python3
"""
pages: assemble an image repository's GitHub Pages site from its GitHub Releases (CONVENTIONS §6, "Repo Pages").

The site is the browser-readable home of each build's heavy files: the newest KEEP releases of every live branch,
each with its file trees and package documents as the Release has them and its test tarballs unpacked as the hub
unpacks them. Nothing is committed anywhere: every run starts from an empty directory and reads the Releases, so
the last deploy is complete whatever an earlier one missed.

    python3 pages.py assemble --out <dir> [--repo owner/repo] [--keep 5]

A branch is live when its own image.yml names it in `release.branch`. A release belongs to the branch its
`target_commitish` names; a release with a sha target to the branch whose release tag prefixes its tag (longest
first), else to the default branch. A release whose target names a branch that is no longer live is left out.

API calls go through `gh` (GH_TOKEN): one GraphQL query for the branches and their image.yml, the paginated
release list. Assets are downloaded from their browser_download_url with the token, which costs no REST quota
(the API asset endpoint would spend one request each, and webtop alone has over 600 assets in its window).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import io
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import gha

Json = dict[str, Any]
Fetch = Callable[[str], "bytes | None"]

SCHEMA = "lsio-pages/1"
KEEP = 5
ARCHES = ("amd64", "arm64")
MAX_TARBALL_BYTES = 64 * 1024 * 1024
"""Extracted bytes allowed per tarball, as the hub allows; anything beyond stays on the Release."""
MAX_SITE_BYTES = 900 * 1024 * 1024
"""Pages refuses a site over 1 GB. Above this the oldest builds that are not a branch's newest are dropped."""
REPORT_FILES = {
    "screenshot": "screenshot.png",
    "container_log": "container.log",
    "browser_log": "browser.log",
    "sysinfo": "sysinfo.txt",
    "shellcheck": "shellcheck.xml",
}
"""test.<arch>.json `files` keys and the fixed names they get under <arch>/ (the hub's build report names)."""
EXTRA_FILES = ("package_diff.txt",)
RETIRED = "index.html"
"""The harness's old HTML report, still in older tarballs: never unpacked, as the hub never unpacks it."""
TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
SHA = re.compile(r"[0-9a-f]{40}")
ATTEMPTS = 4

BRANCHES_QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name }
    refs(refPrefix: "refs/heads/", first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { name target { ... on Commit { file(path: "image.yml") { object { ... on Blob { text } } } } } }
    }
  }
}
"""


def _pause(attempt: int) -> None:
    time.sleep(min(60, 5 * 2**attempt))


def gh_json(args: list[str]) -> Any:
    """`gh api <args>` as JSON, retried: the release path must not lose its site to one flaky call."""
    for attempt in range(ATTEMPTS):
        proc = subprocess.run(["gh", "api", *args], capture_output=True, text=True, check=False)
        if proc.returncode == 0:
            return json.loads(proc.stdout or "null")
        if attempt + 1 == ATTEMPTS:
            raise RuntimeError(f"gh api {args[0]} failed: {proc.stderr.strip()[:500]}")
        print(f"retry: gh api {args[0]} failed (attempt {attempt + 1}/{ATTEMPTS})", file=sys.stderr)
        _pause(attempt)
    raise AssertionError("unreachable")


def read_branches(repo: str) -> tuple[str, dict[str, str | None]]:
    """The default branch and {branch: image.yml text or None} for every branch, in one query per 100 branches."""
    owner, name = repo.split("/", 1)
    texts: dict[str, str | None] = {}
    default = ""
    after: str | None = None
    while True:
        args = ["graphql", "-f", f"query={BRANCHES_QUERY}", "-f", f"owner={owner}", "-f", f"name={name}"]
        if after:
            args += ["-f", f"after={after}"]
        data = (gh_json(args) or {}).get("data", {}).get("repository") or {}
        default = ((data.get("defaultBranchRef") or {}).get("name")) or default
        refs = data.get("refs") or {}
        for node in refs.get("nodes") or []:
            obj = ((node.get("target") or {}).get("file") or {}).get("object") or {}
            texts[node["name"]] = obj.get("text")
        page = refs.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        after = page.get("endCursor")
    if not default:
        raise RuntimeError(f"could not read the branches of {repo}")
    return default, texts


def live_branches(default: str, texts: dict[str, str | None]) -> dict[str, str]:
    """{branch: release tag} for every branch whose own image.yml names it in release.branch."""
    import yaml  # noqa: PLC0415 - installed by the step when the runner lacks it

    live: dict[str, str] = {}
    for branch, text in texts.items():
        if not text:
            continue
        try:
            release = (yaml.safe_load(text) or {}).get("release") or {}
        except (yaml.YAMLError, AttributeError):
            gha.warning(f"image.yml on {branch} does not parse; its builds are left out of Pages")
            continue
        if isinstance(release, dict) and release.get("branch") == branch:
            live[branch] = str(release.get("tag") or ("latest" if branch == default else branch))
    return live


def list_releases(repo: str) -> list[Json]:
    pages = gh_json(["--paginate", "--slurp", f"repos/{repo}/releases?per_page=100"]) or []
    return [release for page in pages for release in page]


def branch_of(release: Json, live: dict[str, str], default: str) -> str | None:
    """The live branch a release belongs to (module docstring), or None."""
    target = str(release.get("target_commitish") or "")
    if target in live:
        return target
    if target and not SHA.fullmatch(target):
        return None  # made from a branch that is no longer live
    tag = str(release.get("tag_name") or "")
    prefixed = sorted(((t, b) for b, t in live.items() if t != "latest"), key=lambda tb: len(tb[0]), reverse=True)
    for release_tag, branch in prefixed:
        if tag.startswith(f"{release_tag}-"):
            return branch
    return default if default in live and re.fullmatch(r".+-ls\d+", tag) else None


def _published(release: Json) -> str:
    return str(release.get("published_at") or release.get("created_at") or "")


def select(releases: list[Json], live: dict[str, str], default: str, keep: int = KEEP) -> list[tuple[str, Json]]:
    """(branch, release) for the newest `keep` published releases of each live branch, newest first overall."""
    by_branch: dict[str, list[Json]] = {}
    for release in releases:
        if release.get("draft") or not TAG.fullmatch(str(release.get("tag_name") or "")):
            continue
        branch = branch_of(release, live, default)
        if branch is not None:
            by_branch.setdefault(branch, []).append(release)
    chosen = [
        (branch, release)
        for branch, items in by_branch.items()
        for release in sorted(items, key=_published, reverse=True)[:keep]
    ]
    return sorted(chosen, key=lambda br: _published(br[1]), reverse=True)


def _member_path(member: tarfile.TarInfo) -> str | None:
    """The normalised relative path of a regular file, or None for anything that escapes the archive root."""
    if not member.isreg():
        return None
    path = posixpath.normpath(member.name)
    if path.startswith(("/", "../")) or path in (".", ".."):
        return None
    return path


def unpack(data: bytes, report: Json | None) -> dict[str, bytes]:
    """
    The tarball's regular files as {name under <arch>/: bytes}, exactly as the hub's build reports unpack it
    (hub lsio_hub/generate/builds.py): the report's named files under their fixed names, the extra files by
    basename, everything else under its path in the archive (one leading directory stripped), never index.html.
    Stops at MAX_TARBALL_BYTES.
    """
    out: dict[str, bytes] = {}
    named = {v: REPORT_FILES[k] for k, v in (report or {}).get("files", {}).items() if k in REPORT_FILES}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = [(m, p) for m in tar.getmembers() if (p := _member_path(m)) is not None]
        roots = {p.split("/", 1)[0] for _, p in members if "/" in p}
        strip = next(iter(roots)) + "/" if len(roots) == 1 and all("/" in p for _, p in members) else ""
        for member, path in members:
            short = path[len(strip) :] if strip and path.startswith(strip) else path
            base = posixpath.basename(short)
            target = named.get(path) or named.get(short) or (base if base in EXTRA_FILES else short)
            if target == RETIRED:
                continue
            if total + member.size > MAX_TARBALL_BYTES:
                break
            handle = tar.extractfile(member)
            if handle is None:
                continue
            content = handle.read()
            total += len(content)
            out[target] = content
    return out


def make_fetch(token: str) -> Fetch:
    """Download a release asset; None when it is gone (404) or still failing after the retries."""

    def fetch(url: str) -> bytes | None:
        for attempt in range(ATTEMPTS):
            request = urllib.request.Request(url, headers={"User-Agent": "lsio-pages"})
            if token:
                # Not sent on to the storage host the download redirects to.
                request.add_unredirected_header("Authorization", f"Bearer {token}")
            try:
                with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - release asset URLs
                    return bytes(response.read())
            except urllib.error.HTTPError as err:
                if err.code == 404:
                    return None
                error: Exception = err
            except (OSError, TimeoutError) as err:
                error = err
            if attempt + 1 < ATTEMPTS:
                _pause(attempt)
        gha.warning(f"could not download {url}: {error}")
        return None

    return fetch


def _write(root: Path, relative: str, content: bytes) -> int:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return len(content)


def build_files(release: Json, fetch: Fetch) -> tuple[list[str], dict[str, bytes]]:
    """The arches a release has and its files under builds/<tag>/."""
    assets = {a.get("name"): a.get("browser_download_url") for a in release.get("assets") or [] if a.get("name")}
    files: dict[str, bytes] = {}
    arches: list[str] = []
    for arch in ARCHES:
        names = (f"files.{arch}.json.gz", f"packages.{arch}.json.gz", f"test.{arch}.tar.gz")
        if not any(assets.get(n) for n in names):
            continue
        arches.append(arch)
        for name in names[:2]:
            if assets.get(name) and (data := fetch(assets[name])) is not None:
                files[name] = data
        tarball = assets.get(f"test.{arch}.tar.gz")
        if tarball and (data := fetch(tarball)) is not None:
            report_url = assets.get(f"test.{arch}.json")
            report_bytes = fetch(report_url) if report_url else None
            try:
                report = json.loads(report_bytes) if report_bytes else None
            except ValueError:
                report = None
            try:
                unpacked = unpack(data, report if isinstance(report, dict) else None)
            except (tarfile.TarError, OSError, EOFError) as err:
                gha.warning(f"{release.get('tag_name')}: test.{arch}.tar.gz does not unpack: {err}")
                unpacked = {}
            files.update({f"{arch}/{name}": content for name, content in unpacked.items()})
    return arches, files


def _trim(out: Path, builds: list[Json], sizes: dict[str, int], limit: int) -> list[Json]:
    """Drop the oldest builds that are not their branch's newest until the site fits under `limit`."""
    total = sum(sizes.values())
    newest = {}
    for build in builds:
        newest.setdefault(build["branch"], build["tag"])
    for build in reversed(list(builds)):
        if total <= limit:
            break
        if newest[build["branch"]] == build["tag"]:
            continue
        shutil.rmtree(out / "builds" / build["tag"], ignore_errors=True)
        total -= sizes.pop(build["tag"], 0)
        builds = [b for b in builds if b["tag"] != build["tag"]]
        gha.warning(f"Pages: {build['tag']} left out to stay under the size limit; it is on its Release")
    return builds


def assemble(
    out: Path,
    repo: str,
    selected: list[tuple[str, Json]],
    fetch: Fetch,
    keep: int = KEEP,
    now: str | None = None,
    limit: int = MAX_SITE_BYTES,
    workers: int = 8,
) -> Json:
    """Write the site into `out` (emptied first) and return index.json."""
    if out.exists():
        shutil.rmtree(out)
    (out / "builds").mkdir(parents=True)
    builds: list[Json] = []
    sizes: dict[str, int] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda br: build_files(br[1], fetch), selected))
    for (branch, release), (arches, files) in zip(selected, results, strict=True):
        tag = str(release["tag_name"])
        sizes[tag] = sum(_write(out / "builds" / tag, name, content) for name, content in files.items())
        builds.append({"tag": tag, "branch": branch, "published_at": _published(release), "arches": arches})
    builds = _trim(out, builds, sizes, limit)
    index = {
        "schema": SCHEMA,
        "repo": repo,
        "generated_at": now or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "keep": keep,
        "builds": builds,
    }
    (out / "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    return index


def _size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    a = sub.add_parser("assemble", help="write the Pages site of a repository into --out")
    a.add_argument("--out", required=True, type=Path)
    a.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    a.add_argument("--keep", type=int, default=KEEP)
    args = ap.parse_args(argv)
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo):
        ap.error("--repo owner/repo is required")

    default, texts = read_branches(args.repo)
    live = live_branches(default, texts)
    selected = select(list_releases(args.repo), live, default, args.keep)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    index = assemble(args.out, args.repo, selected, make_fetch(token), args.keep)
    size = _size(args.out)
    owner, name = args.repo.split("/", 1)
    url = f"https://{owner.lower()}.github.io/{name}"
    print(f"{len(index['builds'])} builds of {len(live)} live branches, {size / 1024 / 1024:.1f} MiB in {args.out}")
    gha.set_outputs({"dir": str(args.out), "builds": len(index["builds"]), "bytes": size, "url": url})
    gha.summary(f"Pages: {len(index['builds'])} builds of {len(live)} live branches, {size / 1024 / 1024:.1f} MiB, {url}/")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
