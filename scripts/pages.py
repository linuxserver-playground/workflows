#!/usr/bin/env python3
"""
pages: assemble an image repository's GitHub Pages site from its GitHub Releases (CONVENTIONS §6, "Repo Pages").

The site is the browser-readable home of the newest KEEP releases of every live branch: copies of their documents
(build.json as attached; report.json, tests.json, diff.json, layers.<arch>.json and packages.<arch>.json made by
builder.derive.documents, the code the hub's API renders them with, from record.json, or from the other assets for a
release without one; the hub stays their API and nothing depends on these copies), vulns.json (the scan
record.json holds, shaped by builder.derive.vulns.document, the state of the image as it was released), the file trees and syft documents as the Release has them, and its test tarballs unpacked as the hub unpacked them. The
builder package is installed by the action. Nothing is committed anywhere: every run starts from an empty directory
and reads the Releases, so the last deploy is complete whatever an earlier one missed.

    python3 pages.py assemble --out <dir> [--repo owner/repo] [--keep 5] [--site-url URL]

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
import http.client
import io
import json
import os
import posixpath
import re
import shutil
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import gha
import netretry

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


def gh_json(args: list[str]) -> Any:
    """`gh api <args>` as JSON, retried on network failures: the release path must not lose its site to one flaky call."""
    proc = netretry.run(["gh", "api", *args])
    if proc.returncode != 0:
        raise RuntimeError(f"gh api {args[0]} failed: {proc.stderr.strip()[:500]}")
    return json.loads(proc.stdout or "null")


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
    """Download a release asset, retried on network failures; None when it is gone (404) or fails otherwise."""

    def download(url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": "lsio-pages"})
        if token:
            # Not sent on to the storage host the download redirects to.
            request.add_unredirected_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - release asset URLs
            return bytes(response.read())

    def fetch(url: str) -> bytes | None:
        try:
            return netretry.retry(lambda: download(url), what=f"GET {url}")
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return None
            error: Exception = err
        except (OSError, TimeoutError, http.client.HTTPException) as err:
            error = err
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


def vulns_json(record_bytes: bytes | None, image: str, tag: str) -> bytes | None:
    """
    vulns.json from a release's record.json: its own scan (`vulns`, per architecture), shaped as the contract says
    (CONVENTIONS §6). None when there is no record, it does not parse, or it holds no scan.
    """
    from builder.derive.vulns import document  # noqa: PLC0415 - installed by the action, not needed to select

    try:
        record = json.loads(record_bytes) if record_bytes else None
    except ValueError:
        return None
    scans = record.get("vulns") if isinstance(record, dict) else None
    if not isinstance(scans, dict) or not scans:
        return None
    doc = document(str(record.get("image") or image), str(record.get("tag") or tag), scans)
    return (json.dumps(doc, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _image_name(repo: str) -> str:
    return repo.rsplit("/", 1)[-1].removeprefix("docker-")


def _json_bytes(doc: Any) -> bytes:
    return (json.dumps(doc, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _parse(data: bytes | None) -> Any:
    try:
        return json.loads(data) if data else None
    except (ValueError, UnicodeDecodeError):
        return None


def pages_url(repo: str) -> str:
    """The repository's Pages site, `https://<owner>.github.io/<repo>`, without a trailing slash."""
    owner, name = repo.split("/", 1)
    return f"https://{owner.lower()}.github.io/{name}"


class Releases:
    """
    What the documents of one release need from the others: the previous release of its branch, by the tag build.json
    names (`previous.tag`), for the diff of a release without a record.json (`builder.derive.documents.parts`), and
    the base image layers the registry publishes, once per base digest. Read only when a release needs them.
    """

    def __init__(self, releases: list[Json], fetch: Fetch, fetch_base: Callable[[str, str | None], Json] | None) -> None:
        self.by_tag = {str(r.get("tag_name")): r for r in releases if r.get("tag_name")}
        self.fetch = fetch
        self._fetch_base = fetch_base
        self._bases: dict[str, Json | Exception] = {}
        self._lock = threading.Lock()

    def previous(self, tag: str) -> tuple[Json, dict[str, list[Json]] | None] | None:
        """build.json of the release `tag` and its package rows per architecture (its record.json's, else its syft
        documents normalised), or None when it has no readable build.json."""
        from builder.derive.packages import normalise_gz  # noqa: PLC0415 - installed by the action

        release = self.by_tag.get(tag)
        if release is None:
            return None
        assets = _assets(release)
        bundle = _parse(self.fetch(assets["build.json"])) if assets.get("build.json") else None
        if not isinstance(bundle, dict):
            return None
        record = _parse(self.fetch(assets["record.json"])) if assets.get("record.json") else None
        if isinstance(record, dict) and record.get("tag") == tag and record.get("packages"):
            return bundle, dict(record["packages"])
        rows: dict[str, list[Json]] = {}
        for arch in ARCHES:
            url = assets.get(f"packages.{arch}.json.gz")
            if url and (data := self.fetch(url)) is not None:
                try:
                    rows[arch] = normalise_gz(data)
                except (OSError, ValueError, EOFError):
                    continue
        return bundle, rows or None

    def base(self, image: str, digest: str | None) -> Json:
        """The base image's layer digests from its registry (`builder.derive.registry.base_layers`), cached."""
        key = f"{image}@{digest}"
        with self._lock:
            known = self._bases.get(key)
        if known is None:
            if self._fetch_base is None:
                raise LookupError("no registry lookups")
            try:
                known = self._fetch_base(image, digest)
            except Exception as err:  # noqa: BLE001 - remembered, so a refusing registry is asked once per run
                known = err
            with self._lock:
                self._bases[key] = known
        if isinstance(known, Exception):
            raise known
        return known


def _assets(release: Json) -> dict[str, str]:
    return {str(a["name"]): str(a["browser_download_url"]) for a in release.get("assets") or [] if a.get("name")}


def build_documents(
    release: Json, read: Callable[[str], bytes | None], releases: Releases, *, root: str, site: str
) -> dict[str, bytes]:
    """
    The release's documents under builds/<tag>/ (CONVENTIONS §6): build.json as attached, and diff.json, tests.json,
    layers.<arch>.json, packages.<arch>.json, report.json and vulns.json from builder.derive (`documents`, `vulns`),
    from record.json where the build derived them and the other assets where it did not. Empty for a release without
    a readable build.json: its files are still served, its documents are not.
    """
    from builder.derive import documents  # noqa: PLC0415 - installed by the action, not needed to select

    tag = str(release.get("tag_name") or "")
    raw = read("build.json")
    bundle = _parse(raw)
    if not isinstance(bundle, dict) or (bundle.get("version") or {}).get("tag") != tag:
        gha.warning(f"{tag}: no readable build.json for this release; its documents are left out of Pages")
        return {}
    record = _parse(read("record.json"))
    files: dict[str, bytes] = {"build.json": raw or b""}
    vulns = vulns_json(_json_bytes(record) if isinstance(record, dict) else None, str(bundle["image"]), tag)
    if vulns is not None:
        files["vulns.json"] = vulns
    try:
        parts = documents.parts(bundle, record, read, previous=releases.previous, fetch_base=releases.base)
        for note in parts.notes:
            print(f"pages: {note}", file=sys.stderr)
        docs = documents.documents(
            bundle,
            pages=f"{root}/builds/{tag}/",
            site=site,
            diff=parts.diff,
            reports=parts.reports,
            layer_rows=parts.layer_rows,
            package_rows=parts.package_rows,
            base=parts.base,
            release=str(release.get("html_url") or "") or None,
            vulns=vulns is not None,
        )
    except (KeyError, TypeError, ValueError) as err:
        gha.warning(f"{tag}: its documents could not be made ({type(err).__name__}: {err}); only build.json is served")
        return files
    files.update({name: _json_bytes(doc) for name, doc in docs.items()})
    return files


def build_files(
    release: Json,
    fetch: Fetch,
    image: str = "",
    *,
    releases: Releases | None = None,
    root: str = "",
    site: str = "",
) -> tuple[list[str], dict[str, bytes]]:
    """The arches a release has and its files under builds/<tag>/: its documents, file trees, syft documents and test
    reports. Every asset is downloaded at most once."""
    assets = _assets(release)
    cache: dict[str, bytes | None] = {}

    def read(name: str) -> bytes | None:
        if name not in cache:
            cache[name] = fetch(assets[name]) if assets.get(name) else None
        return cache[name]

    files: dict[str, bytes] = {}
    if releases is not None:
        files.update(build_documents(release, read, releases, root=root, site=site))
    elif assets.get("record.json"):
        tag = str(release.get("tag_name") or "")
        try:
            vulns = vulns_json(read("record.json"), image, tag)
        except (KeyError, TypeError, ValueError) as err:
            gha.warning(f"{tag}: record.json has no usable scan: {err}")
            vulns = None
        if vulns is not None:
            files["vulns.json"] = vulns
    arches: list[str] = []
    for arch in ARCHES:
        names = (f"files.{arch}.json.gz", f"packages.{arch}.json.gz", f"test.{arch}.tar.gz")
        if not any(assets.get(n) for n in names):
            continue
        arches.append(arch)
        for name in names[:2]:
            if (data := read(name)) is not None:
                files[name] = data
        if (data := read(f"test.{arch}.tar.gz")) is not None:
            report = _parse(read(f"test.{arch}.json"))
            try:
                unpacked = unpack(data, report if isinstance(report, dict) else None)
            except (tarfile.TarError, OSError, EOFError) as err:
                gha.warning(f"{release.get('tag_name')}: test.{arch}.tar.gz does not unpack: {err}")
                unpacked = {}
            files.update({f"{arch}/{name}": content for name, content in unpacked.items()})
            cache.pop(f"test.{arch}.tar.gz", None)  # the tarballs are the big ones: not kept once unpacked
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
    releases: Releases | None = None,
    site: str = "",
) -> Json:
    """Write the site into `out` (emptied first) and return index.json. With `releases`, each build's documents too."""
    if out.exists():
        shutil.rmtree(out)
    (out / "builds").mkdir(parents=True)
    builds: list[Json] = []
    sizes: dict[str, int] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(
            pool.map(
                lambda br: build_files(br[1], fetch, _image_name(repo), releases=releases, root=pages_url(repo), site=site),
                selected,
            )
        )
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
    a.add_argument(
        "--site-url",
        default=os.environ.get("SITE_URL") or "",
        help="the website's root, which the documents link (default https://<owner>.github.io/site/)",
    )
    args = ap.parse_args(argv)
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo):
        ap.error("--repo owner/repo is required")

    from builder.derive.registry import base_layers  # noqa: PLC0415 - installed by the action

    default, texts = read_branches(args.repo)
    live = live_branches(default, texts)
    every = list_releases(args.repo)
    selected = select(every, live, default, args.keep)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    fetch = make_fetch(token)
    owner = args.repo.split("/", 1)[0]
    site = args.site_url or f"https://{owner.lower()}.github.io/site/"
    started = time.monotonic()
    index = assemble(
        args.out, args.repo, selected, fetch, args.keep, releases=Releases(every, fetch, base_layers), site=site
    )
    size = _size(args.out)
    url = pages_url(args.repo)
    print(f"assembled in {time.monotonic() - started:.1f}s", file=sys.stderr)
    print(f"{len(index['builds'])} builds of {len(live)} live branches, {size / 1024 / 1024:.1f} MiB in {args.out}")
    gha.set_outputs({"dir": str(args.out), "builds": len(index["builds"]), "bytes": size, "url": url})
    gha.summary(f"Pages: {len(index['builds'])} builds of {len(live)} live branches, {size / 1024 / 1024:.1f} MiB, {url}/")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
