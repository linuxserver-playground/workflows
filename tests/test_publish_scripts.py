"""emit_bundle, release_notes, notify, pr_comment and pr_cleanup."""

from __future__ import annotations

import gzip
import hashlib
import hmac
import json
from pathlib import Path

import pytest

import emit_bundle
import notify
import pr_cleanup
import pr_comment
import release_notes

try:
    import contracts
except ImportError:  # pragma: no cover
    contracts = None

SHA = "0123456789abcdef0123456789abcdef01234567"
D1 = "sha256:" + "a" * 64
D2 = "sha256:" + "b" * 64
BASE = "sha256:" + "c" * 64
INDEX = "sha256:" + "d" * 64


def prepare_outputs(mode: str = "live") -> dict:
    return {
        "name": "example",
        "mode": mode,
        "branch": "main",
        "sha": SHA,
        "pr_number": "12" if mode == "pr" else "",
        "platforms": '["amd64","arm64"]',
        "base": "alpine",
        "base_image": "ghcr.io/linuxserver/baseimage-alpine:3.22",
        "test_enabled": "true",
        "upstream": "v1.2.3",
        "upstream_clean": "v1.2.3",
        "semver": "1.2.3",
        "source": "github_release",
        "source_url": "https://github.com/o/r/releases/tag/v1.2.3",
        "release_tag": "latest",
        "ls_build": "4" if mode == "live" else "0",
        "primary_tag": "v1.2.3-ls4" if mode == "live" else "pr-12-0123456",
        "all_tags": '["latest","v1.2.3-ls4","amd64-latest"]',
        "previous_tag": "v1.2.2-ls3" if mode == "live" else "",
        "previous_release_url": "https://github.com/o/docker-example/releases/tag/v1.2.2-ls3" if mode == "live" else "",
    }


def write_out_dir(out: Path, arches=("amd64", "arm64"), test_ok=True) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for i, a in enumerate(arches):
        digest = [D1, D2][i]
        (out / f"arch.{a}.json").write_text(
            json.dumps(
                {
                    "arch": a,
                    "digest": digest,
                    "size": 1000 + i,
                    "uncompressed_size": 5000,
                    "layers": 3,
                    "runner": "Linux-X64",
                    "build_seconds": 12.5,
                    "base_image": "ghcr.io/linuxserver/baseimage-alpine:3.22",
                    "base_digest": BASE,
                    "labels": {"org.opencontainers.image.title": "Example", "io.linuxserver.image.mode": "live"},
                    "dockerfile": "FROM x\n\nENV LSIO_FIRST_PARTY=true\n",
                }
            )
        )
        (out / f"test.{a}.json").write_text(json.dumps({"schema": 1, "image": "x", "tag": "t", "arch": a, "started": "2026-10-24T00:00:00Z", "finished": "2026-10-24T00:01:00Z", "success": test_ok, "tests": [{"name": "Container startup", "status": "PASS" if test_ok else "FAIL"}]}))
        (out / f"image-config.{a}.json").write_text(json.dumps([{"Config": {"Env": ["PATH=/usr/bin", "LSIO_FIRST_PARTY=true"], "ExposedPorts": {"8080/tcp": {}}, "Volumes": {"/config": {}}, "Entrypoint": ["/init"], "Cmd": None, "User": "", "WorkingDir": ""}}]))
        with gzip.open(out / f"packages.{a}.json.gz", "wt") as fh:
            json.dump({"artifacts": [{"type": "apk", "name": "jq"}, {"type": "apk", "name": "bash"}, {"type": "binary", "name": "x"}], "distro": {"prettyName": "Alpine Linux v3.22"}}, fh)
        (out / f"package_versions.{a}.txt").write_text("NAME VERSION TYPE\njq 1.7 apk\n")
        (out / f"layers.{a}.json").write_text("[]")
        (out / f"files.{a}.json.gz").write_bytes(gzip.compress(b"[]"))
        (out / f"test.{a}.tar.gz").write_bytes(b"\x1f\x8b\x08\x00")
    (out / "init-graph.amd64.json").write_text(json.dumps({"d2": "a -> b", "units": [{"name": "init-example-config", "type": "oneshot", "depends_on": ["init-config"], "source": "image", "script": "#!/bin/bash"}, {"name": "init-config", "type": "oneshot", "depends_on": []}]}))


def _validate(kind: str, data: dict) -> list[str]:
    if contracts is None:
        pytest.skip("contracts package not installed")
    return [contracts.format_error(e) for e in contracts.errors(kind, data)]


def test_bundle_live_is_valid(tmp_path):
    out = tmp_path / "out"
    write_out_dir(out)
    bundle = emit_bundle.build_bundle(prepare_outputs(), out, repository="o/docker-example", run_id=42, run_url="https://github.com/o/docker-example/actions/runs/42", workflows_ref="v1", ci_image="ghcr.io/linuxserver-playground/ci:latest", reason="upstream", index_digest=INDEX, control_url="https://control.example.org")
    assert _validate("bundle", bundle) == []
    assert bundle["version"] == {"upstream": "v1.2.3", "upstream_clean": "v1.2.3", "semver": "1.2.3", "release_tag": "latest", "tags": ["latest", "v1.2.3-ls4", "amd64-latest"], "source": "github_release", "source_url": "https://github.com/o/r/releases/tag/v1.2.3", "ls_build": 4, "tag": "v1.2.3-ls4"}
    assert [p["arch"] for p in bundle["platforms"]] == ["amd64", "arm64"]
    assert bundle["platforms"][0]["digest"] == D1 and bundle["platforms"][0]["size"] == 1000
    assert bundle["base"] == {"family": "alpine", "image": "ghcr.io/linuxserver/baseimage-alpine:3.22", "digest": BASE}
    assert bundle["index_digest"] == INDEX
    assert bundle["config"]["exposed_ports"] == ["8080/tcp"] and bundle["config"]["volumes"] == ["/config"]
    assert bundle["dockerfile"]["amd64"].startswith("FROM x")
    assert bundle["test"] == {"enabled": True, "success": True, "per_arch": {"amd64": True, "arm64": True}, "report_url": "https://control.example.org/builds/o/docker-example/42"}
    assert bundle["packages"]["amd64"] == {"count": 3, "distro": "Alpine Linux v3.22", "hash": hashlib.md5(b"NAME VERSION TYPE\njq 1.7 apk\n").hexdigest()[:8], "by_type": {"apk": 2, "binary": 1}}
    assert bundle["init_graph"]["units"][0]["name"] == "init-example-config"
    assert bundle["previous"] == {"tag": "v1.2.2-ls3", "release_url": "https://github.com/o/docker-example/releases/tag/v1.2.2-ls3"}
    assert bundle["workflow"] == {"run_id": 42, "run_url": "https://github.com/o/docker-example/actions/runs/42", "workflows_ref": "v1", "ci_version": "latest"}
    assert sorted(a["name"] for a in bundle["assets"]) == sorted(f"{p.format(a=a)}" for a in ("amd64", "arm64") for p in emit_bundle.ASSET_PATTERNS)
    assert bundle["reason"] == "upstream"


def test_bundle_pr_sets_reason_and_number(tmp_path):
    out = tmp_path / "out"
    write_out_dir(out, test_ok=False)
    bundle = emit_bundle.build_bundle(prepare_outputs("pr"), out, repository="o/docker-example", run_id=1, run_url="https://x/1", workflows_ref="abc", ci_image="ci", reason="push")
    assert _validate("bundle", bundle) == []
    assert bundle["reason"] == "pull_request"
    assert bundle["pull_request"] == 12
    assert bundle["version"]["ls_build"] == 0
    assert bundle["test"]["success"] is False
    assert "previous" not in bundle and "index_digest" not in bundle


def test_bundle_tolerates_missing_arch(tmp_path):
    out = tmp_path / "out"
    write_out_dir(out, arches=("amd64",))
    bundle = emit_bundle.build_bundle(prepare_outputs(), out, repository="o/r", run_id=1, run_url="https://x/1", workflows_ref="v1", ci_image="ci", reason="push")
    assert _validate("bundle", bundle) == []
    by_arch = {p["arch"]: p for p in bundle["platforms"]}
    assert by_arch["amd64"]["result"] == "success" and "digest" in by_arch["amd64"]
    assert by_arch["arm64"] == {"arch": "arm64", "result": "failure"}  # arch job produced nothing
    assert bundle["test"]["success"] is False  # arm64 report missing


def test_emit_bundle_cli_validates(tmp_path, monkeypatch):
    out = tmp_path / "out"
    write_out_dir(out)
    (tmp_path / "prepare.json").write_text(json.dumps(prepare_outputs()))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "gh_out"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    rc = emit_bundle.main(["--out", str(out), "--prepare", str(tmp_path / "prepare.json"), "--repository", "o/docker-example", "--run-id", "5", "--run-url", "https://x/5", "--workflows-ref", "v1", "--ci-image", "ci:1", "--reason", "rebuild"])
    assert rc == 0
    assert json.loads((out / "build.json").read_text())["workflow"]["ci_version"] == "1"
    assert "Build bundle" in (tmp_path / "summary.md").read_text()


# -------------------------------------------------------------------------------------------- release notes


def test_release_body_three_sections():
    body = release_notes.compose("https://control/x", "## What's Changed\n* Add krb5-libs by @a in #1\n", "Fixed stuff", "https://github.com/o/r/releases/tag/v1")
    assert body.index("## CI") < body.index("## LinuxServer changes") < body.index("## Upstream changes")
    assert "What's Changed" not in body
    assert "* Add krb5-libs" in body
    assert "Source: https://github.com/o/r/releases/tag/v1" in body
    assert body.rstrip().endswith("Fixed stuff")


def test_release_body_defaults_and_truncation():
    body = release_notes.compose("", "", "x" * 70_000, "")
    assert "No test report." in body and "No changes" in body and "_(truncated)_" in body


def test_patch_bundle_adds_notes_and_urls(tmp_path):
    bundle = {"assets": [{"name": "build.json", "size": 1, "sha256": "0" * 64}, {"name": "test.amd64.json", "size": 2, "sha256": "1" * 64}]}
    out = release_notes.patch_bundle(bundle, {"linuxserver": "ls", "upstream": "up", "upstream_url": "https://u"}, "https://github.com/o/r/releases/download/v1-ls1/")
    assert out["notes"] == {"linuxserver": "ls", "upstream": "up", "upstream_url": "https://u"}
    assert out["assets"][1]["url"] == "https://github.com/o/r/releases/download/v1-ls1/test.amd64.json"
    assert "notes" not in release_notes.patch_bundle({"assets": []}, {}, "")


# -------------------------------------------------------------------------------------------- notify


def test_notify_event_shape_and_signature(tmp_path):
    out = tmp_path / "out"
    write_out_dir(out)
    bundle = emit_bundle.build_bundle(prepare_outputs(), out, repository="o/docker-example", run_id=42, run_url="https://x/42", workflows_ref="v1", ci_image="ci", reason="push", index_digest=INDEX)
    env = {"EVENT_TYPE": "build.summary", "REPOSITORY": "o/docker-example", "RUN_ID": "42", "RUN_URL": "https://x/42", "IMAGE": "example", "BRANCH": "main", "MODE": "live", "REASON": "push", "RESULT": "success", "RELEASE_URL": "https://github.com/o/docker-example/releases/tag/v1.2.3-ls4"}
    event = notify.build_event(env, bundle, sent="2026-10-24T00:00:00Z")
    assert _validate("control-event", event) == []
    assert event["bundle"]["image"] == "example"
    failed = notify.build_event({**env, "RESULT": "failure", "FAILED_JOB": "arch", "PR_NUMBER": "3", "MODE": "pr"}, None)
    assert _validate("control-event", failed) == []
    assert failed["bundle"] is None and failed["failed_job"] == "arch" and failed["pull_request"] == 3
    body = b'{"a":1}'
    assert notify.sign(body, "s3cret") == "sha256=" + hmac.new(b"s3cret", body, "sha256").hexdigest()


def test_notify_skips_without_url(monkeypatch, capsys):
    monkeypatch.delenv("CONTROL_URL", raising=False)
    assert notify.main() == 0
    assert "not notifying" in capsys.readouterr().out


def test_notify_posts_signed_body(monkeypatch, tmp_path):
    calls = {}

    def fake_post(url, body, signature, timeout=30):
        calls["url"] = url
        calls["body"] = body
        calls["sig"] = signature
        return 202

    monkeypatch.setattr(notify, "post", fake_post)
    for k, v in {"CONTROL_URL": "https://control.example.org/", "CONTROL_EVENTS_SECRET": "k", "REPOSITORY": "o/r", "RUN_ID": "7", "RESULT": "cancelled"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("BUNDLE_PATH", raising=False)
    assert notify.main() == 0
    assert calls["url"] == "https://control.example.org/events"
    assert calls["sig"] == notify.sign(calls["body"], "k")
    assert json.loads(calls["body"])["result"] == "cancelled"


# -------------------------------------------------------------------------------------------- pr comment / cleanup


def test_pr_comment_compose(tmp_path):
    out = tmp_path / "out"
    write_out_dir(out)
    bundle = emit_bundle.build_bundle(prepare_outputs("pr"), out, repository="o/r", run_id=1, run_url="https://x/1", workflows_ref="v1", ci_image="ci", reason="push")
    body = pr_comment.compose(bundle, "ghcr.io/o/untrusted-pr/example", "pr-12-0123456", True, "https://x/1", "bundle")
    assert body.startswith(pr_comment.MARKER)
    assert "docker pull ghcr.io/o/untrusted-pr/example:pr-12-0123456" in body
    assert "unreviewed pull request build" in body
    assert "| amd64 | `sha256:aaaaaaaaaaaa` | 0.0 MiB | 3 | pass |" in body
    fork = pr_comment.compose(bundle, "ghcr.io/o/untrusted-pr/example", "pr-12-0123456", False, "https://x/1", "bundle")
    assert "comes from a fork" in fork and "docker pull" not in fork


def test_pr_cleanup_selects_versions():
    versions = [
        {"id": 1, "metadata": {"container": {"tags": ["pr-12-abc1234", "amd64-pr-12-abc1234"]}}},
        {"id": 2, "metadata": {"container": {"tags": ["pr-120-abc1234"]}}},
        {"id": 3, "metadata": {"container": {"tags": ["universal-foo-pr-12"]}}},
        {"id": 4, "metadata": {"container": {"tags": []}}},
        {"id": 5, "metadata": {"container": {"tags": ["latest"]}}},
    ]
    assert [v["id"] for v in pr_cleanup.select_versions(versions, 12)] == [1, 3]
