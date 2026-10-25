"""pr_report: escaping, the untrusted-artifact readers, the comment, and resolving events to pull requests."""

from __future__ import annotations

import io
import struct
import tarfile
import zlib
from pathlib import Path
from typing import Any

import pytest

import pr_report as r

REPO = "linuxserver-playground/docker-example"
SHA = "0123456789abcdef0123456789abcdef01234567"
DIGEST = "sha256:" + "a" * 64


def png(width: int = 4, height: int = 2) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\x10\x20\x30" * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def tarball(path: Path, members: dict[str, bytes], symlink: str | None = None) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if symlink:
            info = tarfile.TarInfo(symlink)
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
            tar.addfile(info)
    return path


# ------------------------------------------------------------------------------------------------ escaping


def test_text_neutralises_markdown_html_and_mentions() -> None:
    out = r.text("<img src=x onerror=alert(1)> @maintainer | [click](https://evil) `x`\nnext\x1b[31m")
    assert "<" not in out and ">" not in out
    assert "@maintainer" not in out and "@​maintainer" in out
    assert "\\|" in out and "\\[click\\]" in out and "\\`x\\`" in out
    assert "\n" not in out and "\x1b" not in out
    assert r.text("a" * 500, 10) == "aaaaaaaaa…"


def test_code_block_fence_outlasts_the_content() -> None:
    block = r.code_block("before\n````\n```` after")
    fence = block.splitlines()[0]
    assert fence == "`````" and block.endswith("\n`````")


def test_code_keeps_only_name_characters() -> None:
    assert r.code("v2.8.3-ls1") == "`v2.8.3-ls1`"
    assert r.code("x`) <b>evil</b>") == "`xbevil/b`"


# ------------------------------------------------------------------------------------------------ readers


def test_tar_member_reads_only_the_named_regular_file(tmp_path: Path) -> None:
    shot = png()
    path = tarball(tmp_path / "t.tar.gz", {"./screenshot.png": shot, "../container.log": b"escape"}, symlink="container.log")
    assert r.tar_member(path, "screenshot.png", r.MAX_PNG) == shot
    assert r.tar_member(path, "container.log", r.MAX_LOG) is None  # only a symlink and a ../ name carry it
    assert r.tar_member(path, "screenshot.png", 10) is None  # over the cap
    assert r.tar_member(tmp_path / "missing.tar.gz", "screenshot.png", 10) is None
    (tmp_path / "junk.tar.gz").write_bytes(b"not a tarball")
    assert r.tar_member(tmp_path / "junk.tar.gz", "screenshot.png", 10) is None


def test_png_size_rejects_anything_else() -> None:
    assert r.png_size(png(640, 480)) == (640, 480)
    assert r.png_size(png(20000, 10)) is None
    assert r.png_size(b"GIF89a....") is None
    assert r.png_size(None) is None


def test_package_diff() -> None:
    old = r.parse_table("NAME VERSION TYPE\nbash 5.2 apk\ncurl 8.0 apk\n")
    new = r.parse_table("NAME VERSION TYPE\nbash 5.3 apk\njq 1.8 apk\n")
    assert r.diff_lines(old, new) == ["~ bash 5.2 -> 5.3", "- curl 8.0", "+ jq 1.8"]


def test_load_arch_survives_hostile_artifacts(tmp_path: Path) -> None:
    (tmp_path / "test.amd64.json").write_text('{"success": "yes", "tests": [{"name": "Container startup", "status": "PASS"}, "junk"], "runtime": []}')
    tarball(tmp_path / "test.amd64.tar.gz", {"screenshot.png": b"\x89PNG but not really", "container.log": b"\n".join(b"line %d" % i for i in range(100))})
    data = r.load_arch(tmp_path, "amd64")
    assert data is not None
    assert data.success is None and data.runtime == {} and data.tests == [{"name": "Container startup", "status": "PASS"}]
    assert data.screenshot is None
    assert data.log_tail.splitlines()[0] == "line 40" and len(data.log_tail.splitlines()) == r.LOG_TAIL
    assert r.load_arch(tmp_path, "arm64") is None


# ------------------------------------------------------------------------------------------------ compose


def context(**overrides: Any) -> r.Context:
    ctx = r.Context(
        image="example",
        primary_tag="pr-12-0123456",
        image_path="ghcr.io/linuxserver-playground/untrusted-pr/example",
        head_sha=SHA,
        run_url="https://github.com/linuxserver-playground/docker-example/actions/runs/1",
        conclusion="success",
        fork=False,
        approved=False,
        published=False,
        image_exists=True,
        base_ref="main",
        base_release="1.2.2-ls3",
        bundle={"version": {"upstream": "1.2.3"}, "platforms": [{"arch": "amd64", "digest": DIGEST, "size": 52428800, "layers": 11}]},
    )
    amd = r.ArchData("amd64", success=True, startup=2.0, packages={"bash": "5.3", "jq": "1.8"})
    amd.tests = [{"name": "Container startup", "status": "PASS", "message": "init done after 2.0s"}, {"name": "Take screenshot", "status": "FAIL", "message": "<b>@bob</b> | broke"}]
    amd.runtime = {"rss_bytes": 104857600, "pids": 9, "listening_tcp": [80, 443]}
    amd.screenshot = png()
    amd.screenshot_url = "https://github.com/user-attachments/assets/e6f9f807-4ff8-446d-88a1-704dd16d74aa"
    amd.log_tail = "[ls.io-init] done.\n``` sneaky"
    arm = r.ArchData("arm64", success=True, packages={"bash": "5.3"})
    arm.tests = [{"name": "Container startup", "status": "PASS"}]
    ctx.arches = {"amd64": amd, "arm64": arm}
    ctx.base_packages = {"amd64": {"bash": "5.2"}, "arm64": {"bash": "5.3"}}
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


def test_compose_embeds_the_whole_report() -> None:
    body = r.compose(context())
    assert body.startswith(r.MARKER)
    assert "### Build report for `example` · `pr-12-0123456`" in body
    assert "**Build and tests passed** · commit `0123456` · upstream `1.2.3`" in body
    assert "| Test | amd64 | arm64 |" in body
    assert "| Container startup | pass · init done after 2.0s | pass |" in body
    # A hostile test message stays text: no tag, no mention, no table break.
    assert "<b>" not in body and "@bob" not in body and "\\| broke" in body
    assert '<img src="https://github.com/user-attachments/assets/e6f9f807-4ff8-446d-88a1-704dd16d74aa" width="420" alt="amd64 screenshot">' in body
    assert "| amd64 | `sha256:aaaaaaaaaaaa` | 50.0 MiB | 11 | 2 s | 100.0 MiB | 9 | 80, 443 |" in body
    assert "Package changes on amd64 against `1.2.2-ls3`: 2</summary>" in body and "~ bash 5.2 -> 5.3" in body
    assert "Package changes on arm64 against `1.2.2-ls3`: none" in body
    assert "````\n[ls.io-init] done.\n``` sneaky\n````" in body
    assert "docker pull ghcr.io/linuxserver-playground/untrusted-pr/example:pr-12-0123456" in body
    assert "unreviewed pull request build" in body


def test_compose_fork_waiting_for_approval_and_missing_attachments() -> None:
    ctx = context(fork=True, image_exists=False, attachments=False)
    ctx.arches["amd64"].screenshot_url = None
    body = r.compose(ctx)
    assert "docker pull" not in body
    assert "published only after a maintainer adds the `safe-to-publish` label" in body
    assert "Screenshot for amd64 is in `test.<arch>.tar.gz` of the `bundle` artifact (uploads need the ATTACHMENTS_TOKEN secret)" in body


def test_compose_failed_build_without_reports() -> None:
    ctx = context(conclusion="failure", image_exists=False, bundle={}, arches={}, base_release="")
    body = r.compose(ctx)
    assert "**Build failed**" in body and "no test reports" in body and "No image was published" in body


def test_compose_fits_the_comment_limit() -> None:
    ctx = context()
    ctx.arches["amd64"].log_tail = "x" * 50000
    ctx.arches["arm64"].log_tail = "y" * 50000
    body = r.compose(ctx)
    assert len(body) <= r.MAX_BODY and "left out to fit" in body and "docker pull" in body


# ------------------------------------------------------------------------------------------------ resolve


def fake_api(routes: dict[str, Any]):
    def gh_api(args: list[str], body: Any = None) -> Any:
        path = next(a for a in args if not a.startswith("-"))
        for prefix, value in routes.items():
            if path.startswith(prefix):
                return value
        raise RuntimeError(f"unexpected call {path}")

    return gh_api


def pull(number: int = 12, fork: bool = True, labels: tuple[str, ...] = (), state: str = "open", sha: str = SHA) -> dict:
    head_repo = "someone/docker-example" if fork else REPO
    return {
        "number": number,
        "state": state,
        "head": {"sha": sha, "repo": {"full_name": head_repo}},
        "base": {"ref": "main"},
        "labels": [{"name": n} for n in labels],
    }


def build_run(**overrides: Any) -> dict:
    run = {
        "id": 77,
        "event": "pull_request",
        "path": ".github/workflows/build.yml",
        "head_sha": SHA,
        "head_branch": "feature",
        "head_repository": {"owner": {"login": "someone"}},
        "pull_requests": [],
        "status": "completed",
        "conclusion": "success",
        "html_url": "https://github.com/x/actions/runs/77",
    }
    run.update(overrides)
    return run


IMAGE_YML = {"content": "bmFtZTogZXhhbXBsZQo="}  # "name: example"


def test_resolve_fork_build_finds_the_pull_request_by_head(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(r, "gh_api", fake_api({f"repos/{REPO}/pulls?state=open": [pull()], f"repos/{REPO}/contents/image.yml": IMAGE_YML}))
    out = r.resolve("workflow_run", {"workflow_run": build_run()}, REPO)
    assert out["action"] == "report" and out["pr"] == 12 and out["fork"] is True and out["approved"] is False
    assert out["publish"] is False and out["run_id"] == 77
    assert out["image_path"] == "ghcr.io/linuxserver-playground/untrusted-pr/example" and out["primary_tag"] == "pr-12-0123456"


def test_resolve_approved_fork_publishes_only_a_green_build(monkeypatch: pytest.MonkeyPatch) -> None:
    routes = {f"repos/{REPO}/pulls?state=open": [pull(labels=("safe-to-publish",))], f"repos/{REPO}/contents/image.yml": IMAGE_YML}
    monkeypatch.setattr(r, "gh_api", fake_api(routes))
    assert r.resolve("workflow_run", {"workflow_run": build_run()}, REPO)["publish"] is True
    assert r.resolve("workflow_run", {"workflow_run": build_run(conclusion="failure")}, REPO)["publish"] is False


def test_resolve_skips_what_is_not_a_current_pull_request_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(r, "gh_api", fake_api({f"repos/{REPO}/pulls?state=open": [pull(sha="f" * 40)]}))
    assert r.resolve("workflow_run", {"workflow_run": build_run(event="push")}, REPO)["action"] == "skip"
    assert r.resolve("workflow_run", {"workflow_run": build_run(path=".github/workflows/pr-report.yml")}, REPO)["action"] == "skip"
    # The pull request has a newer head than this run: nothing found for this commit.
    assert r.resolve("workflow_run", {"workflow_run": build_run()}, REPO)["action"] == "skip"


def test_resolve_label_waits_for_the_build(monkeypatch: pytest.MonkeyPatch) -> None:
    routes = {
        f"repos/{REPO}/pulls/12": pull(labels=("safe-to-publish",)),
        f"repos/{REPO}/actions/runs": {"workflow_runs": [build_run(status="in_progress", conclusion=None)]},
    }
    monkeypatch.setattr(r, "gh_api", fake_api(routes))
    assert r.resolve("pull_request_target", {"pull_request": {"number": 12}}, REPO)["action"] == "skip"
    routes[f"repos/{REPO}/actions/runs"] = {"workflow_runs": [build_run(id=70, created_at="1"), build_run(id=78, created_at="2")]}
    routes[f"repos/{REPO}/contents/image.yml"] = IMAGE_YML
    out = r.resolve("pull_request_target", {"pull_request": {"number": 12}}, REPO)
    assert out["action"] == "report" and out["run_id"] == 78 and out["publish"] is True


def test_upsert_only_edits_the_bots_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    comments = [{"id": 1, "body": r.MARKER + " forged", "user": {"login": "someone"}}, {"id": 2, "body": r.MARKER, "user": {"login": r.BOT}}]

    def gh_api(args: list[str], body: Any = None) -> Any:
        calls.append(args)
        return comments if "--paginate" in args else {}

    monkeypatch.setattr(r, "gh_api", gh_api)
    assert r.upsert(REPO, 12, "body") == "updated"
    assert calls[-1][:3] == ["--method", "PATCH", f"repos/{REPO}/issues/comments/2"]
    comments.pop()
    assert r.upsert(REPO, 12, "body") == "created"
