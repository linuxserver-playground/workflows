"""pages: live branches, release selection, the hub's tarball layout and the assembled site (CONVENTIONS §6)."""

from __future__ import annotations

import gzip
import io
import json
import tarfile
from pathlib import Path

import pages

REPO = "linuxserver-playground/docker-example"
SHA = "0123456789abcdef0123456789abcdef01234567"
BASE = f"https://github.com/{REPO}/releases/download"


def manifest(branch: str, tag: str) -> str:
    return f"name: example\nrelease:\n  branch: {branch}\n  tag: {tag}\n"


def release(tag: str, target: str, published: str, names: tuple[str, ...] = (), draft: bool = False) -> dict:
    return {
        "tag_name": tag,
        "target_commitish": target,
        "published_at": published,
        "draft": draft,
        "assets": [{"name": n, "browser_download_url": f"{BASE}/{tag}/{n}"} for n in names],
    }


def tarball(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("out/escape")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
    return buf.getvalue()


LIVE = {"master": "latest", "develop": "develop", "develop-next": "develop-next"}


def test_live_branches_are_the_ones_their_own_manifest_names():
    texts = {
        "master": manifest("master", "latest"),
        "develop": manifest("develop", "develop"),
        "feature": manifest("develop", "develop"),  # a dev branch cut from develop
        "docs": None,
        "broken": "release: [",
    }
    assert pages.live_branches("master", texts) == {"master": "latest", "develop": "develop"}


def test_branch_of_follows_target_then_the_longest_tag_prefix():
    assert pages.branch_of(release("1.0-ls1", "master", ""), LIVE, "master") == "master"
    assert pages.branch_of(release("develop-2.0-ls3", "develop", ""), LIVE, "master") == "develop"
    # A sha target (older releases): the tag prefix decides, longest first, then the default branch.
    assert pages.branch_of(release("develop-next-3.0-ls1", SHA, ""), LIVE, "master") == "develop-next"
    assert pages.branch_of(release("develop-2.0-ls3", SHA, ""), LIVE, "master") == "develop"
    assert pages.branch_of(release("1.0-ls2", SHA, ""), LIVE, "master") == "master"
    assert pages.branch_of(release("not-a-build", SHA, ""), LIVE, "master") is None
    # Made from a branch that is no longer live.
    assert pages.branch_of(release("nightly-1.0-ls9", "nightly", ""), LIVE, "master") is None


def test_select_keeps_the_newest_per_live_branch_newest_first():
    releases = [release(f"1.0-ls{n}", "master", f"2026-10-{n:02d}T00:00:00Z") for n in range(1, 9)]
    releases += [release(f"develop-2.0-ls{n}", "develop", f"2026-10-{n:02d}T12:00:00Z") for n in range(1, 3)]
    releases += [release("1.0-ls99", "master", "2026-10-30T00:00:00Z", draft=True)]
    releases += [release("nightly-1-ls1", "nightly", "2026-10-29T00:00:00Z")]
    chosen = pages.select(releases, LIVE, "master", keep=5)
    assert [r["tag_name"] for _, r in chosen] == [
        "1.0-ls8", "1.0-ls7", "1.0-ls6", "1.0-ls5", "1.0-ls4", "develop-2.0-ls2", "develop-2.0-ls1",
    ]
    assert {b for b, _ in chosen} == {"master", "develop"}


def test_unpack_matches_the_hub_layout():
    report = {"files": {"screenshot": "out/shot-amd64.png", "container_log": "out/logs/container.txt"}}
    data = tarball(
        {
            "out/shot-amd64.png": b"png",
            "out/logs/container.txt": b"log",
            "out/nested/package_diff.txt": b"diff",
            "out/index.html": b"<html>",
            "out/sysinfo.txt": b"sys",
            "out/../../etc/evil": b"x",
        }
    )
    assert pages.unpack(data, report) == {
        "screenshot.png": b"png",
        "container.log": b"log",
        "package_diff.txt": b"diff",
        "sysinfo.txt": b"sys",
    }


def test_assemble_writes_the_contract_layout(tmp_path: Path):
    files_gz = gzip.compress(b'{"tree": []}')
    blobs = {
        f"{BASE}/1.0-ls2/files.amd64.json.gz": files_gz,
        f"{BASE}/1.0-ls2/packages.amd64.json.gz": b"pkg-amd64",
        f"{BASE}/1.0-ls2/packages.arm64.json.gz": b"pkg-arm64",
        f"{BASE}/1.0-ls2/test.amd64.tar.gz": tarball({"out/screenshot.png": b"png"}),
        f"{BASE}/1.0-ls2/test.amd64.json": json.dumps({"files": {"screenshot": "out/screenshot.png"}}).encode(),
        f"{BASE}/1.0-ls2/test.arm64.tar.gz": b"not a tarball",
    }
    names = ("files.amd64.json.gz", "packages.amd64.json.gz", "packages.arm64.json.gz", "test.amd64.tar.gz",
             "test.amd64.json", "test.arm64.tar.gz", "build.json")
    selected = [
        ("master", release("1.0-ls2", "master", "2026-10-02T00:00:00Z", names)),
        ("develop", release("develop-2.0-ls1", "develop", "2026-10-01T00:00:00Z", ("build.json",))),
    ]
    out = tmp_path / "site"
    (out / "stale").mkdir(parents=True)
    index = pages.assemble(out, REPO, selected, blobs.get, now="2026-10-28T00:00:00Z", workers=2)
    assert index == {
        "schema": "lsio-pages/1",
        "repo": REPO,
        "generated_at": "2026-10-28T00:00:00Z",
        "keep": 5,
        "builds": [
            {"tag": "1.0-ls2", "branch": "master", "published_at": "2026-10-02T00:00:00Z", "arches": ["amd64", "arm64"]},
            {"tag": "develop-2.0-ls1", "branch": "develop", "published_at": "2026-10-01T00:00:00Z", "arches": []},
        ],
    }
    assert json.loads((out / "index.json").read_text()) == index
    build = out / "builds" / "1.0-ls2"
    assert (build / "files.amd64.json.gz").read_bytes() == files_gz  # as the Release has it, still gzipped
    assert (build / "packages.arm64.json.gz").read_bytes() == b"pkg-arm64"
    assert (build / "amd64" / "screenshot.png").read_bytes() == b"png"
    assert not (out / "stale").exists() and not (build / "arm64").exists()
    assert sorted(p.name for p in out.iterdir()) == ["builds", "index.json"]


def test_assemble_trims_old_builds_over_the_size_limit(tmp_path: Path):
    def rel(tag: str, branch: str, day: int) -> tuple[str, dict]:
        return branch, release(tag, branch, f"2026-10-{day:02d}T00:00:00Z", ("packages.amd64.json.gz",))

    # Newest first, as select() orders them.
    selected = [rel("1.0-ls3", "master", 9), rel("1.0-ls2", "master", 5), rel("develop-1-ls1", "develop", 2),
                rel("1.0-ls1", "master", 1)]

    def fetch(url: str) -> bytes:
        return b"x" * 100

    index = pages.assemble(tmp_path / "site", REPO, selected, fetch, limit=250, workers=1)
    # The oldest build that is not its branch's newest goes first; every branch keeps its newest.
    assert [b["tag"] for b in index["builds"]] == ["1.0-ls3", "develop-1-ls1"]
    assert sorted(p.name for p in (tmp_path / "site" / "builds").iterdir()) == ["1.0-ls3", "develop-1-ls1"]
