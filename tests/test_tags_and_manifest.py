"""tags.compute_tags (CONVENTIONS §3/§4) and the manifest action's plan."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import manifest as manifest_action
import resolve_version as rv
import tags

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "example-image"
SHA = "0123456789abcdef0123456789abcdef01234567"


def test_live_default_branch_tag_set():
    out = tags.compute_tags("live", "latest", "4.0.20.3014", "4.0.20", 1, "master", "abc1234", None, ["amd64", "arm64"])
    assert out["primary_tag"] == "4.0.20.3014-ls1"
    assert out["tags"] == ["latest", "4.0.20.3014-ls1", "version-4.0.20.3014", "4.0.20"]
    assert out["arch_tags"]["arm64"] == ["arm64v8-latest", "arm64v8-4.0.20.3014-ls1", "arm64v8-version-4.0.20.3014", "arm64v8-4.0.20"]
    assert out["arch_primary"] == {"amd64": "amd64-4.0.20.3014-ls1", "arm64": "arm64v8-4.0.20.3014-ls1"}
    assert out["version_label"] == "4.0.20.3014-ls1"
    assert len(out["all_tags"]) == 12


def test_live_other_branch_prefixes_every_tag():
    out = tags.compute_tags("live", "develop", "5.0.1.1234", "5.0.1", 7, "develop", "abc1234", None, ["amd64"])
    assert out["primary_tag"] == "develop-5.0.1.1234-ls7"
    assert out["tags"] == ["develop", "develop-5.0.1.1234-ls7", "develop-version-5.0.1.1234", "develop-5.0.1"]
    assert out["version_label"] == "5.0.1.1234-ls7"


def test_live_without_semver():
    out = tags.compute_tags("live", "latest", "deadbeef", None, 3, "main", "abc1234", None, ["amd64"])
    assert out["tags"] == ["latest", "deadbeef-ls3", "version-deadbeef"]


def test_dev_and_pr_never_get_latest():
    dev = tags.compute_tags("dev", "latest", "1.2.3", "1.2.3", 0, "feature/x", "abc1234", None, ["amd64", "arm64"])
    assert dev["tags"] == ["feature-x-abc1234"]
    assert "latest" not in dev["all_tags"]
    pr = tags.compute_tags("pr", "latest", "1.2.3", "1.2.3", 0, "master", "abc1234", 12, ["amd64"])
    assert pr["tags"] == ["pr-12-abc1234"]
    assert pr["all_tags"] == ["pr-12-abc1234", "amd64-pr-12-abc1234"]
    with pytest.raises(ValueError):
        tags.compute_tags("pr", "latest", "1", None, 0, "m", "abc1234", None, ["amd64"])


def test_tags_cli(capsys):
    assert tags.main(["--mode", "pr", "--release-tag", "latest", "--sha", SHA, "--pr-number", "3", "--platforms", '["amd64"]']) == 0
    assert json.loads(capsys.readouterr().out)["primary_tag"] == "pr-3-0123456"


# -------------------------------------------------------------------------------------------- manifest action


def fixture_manifest() -> dict:
    return rv.load_manifest(FIXTURE / "image.yml")


def test_mode_detection():
    assert manifest_action.detect_mode("pull_request", "main", "main") == "pr"
    assert manifest_action.detect_mode("push", "main", "main") == "live"
    assert manifest_action.detect_mode("workflow_dispatch", "main", "main") == "live"
    assert manifest_action.detect_mode("push", "feature", "main") == "dev"


def test_plan_live():
    out = manifest_action.compute(fixture_manifest(), event_name="push", ref_name="main", repository="linuxserver-playground/docker-example", sha=SHA, pr_number="", fork=False, repo_dir=FIXTURE)
    assert out["mode"] == "live"
    assert out["image_path"] == "ghcr.io/linuxserver-playground/example"
    assert out["matrix"] == {
        "include": [
            {"arch": "amd64", "runner": "ubuntu-24.04", "dockerfile": "Dockerfile", "arch_prefix": "amd64"},
            {"arch": "arm64", "runner": "ubuntu-24.04-arm", "dockerfile": "Dockerfile.aarch64", "arch_prefix": "arm64v8"},
        ]
    }
    assert out["base_image"] == "ghcr.io/linuxserver/baseimage-alpine:3.22"
    assert out["description"].startswith("A minimal LinuxServer image that adds one package")
    assert out["push_images"] is True
    assert out["first_party"] is True
    assert out["test"]["enabled"] is True and out["test"]["startup_timeout"] == 60
    assert json.loads(out["manifest_json"])["name"] == "example"
    assert out["sha7"] == "0123456"


def test_plan_pr_from_fork_and_dev():
    pr = manifest_action.compute(fixture_manifest(), event_name="pull_request", ref_name="main", repository="o/docker-example", sha=SHA, pr_number="7", fork=True, repo_dir=FIXTURE)
    assert pr["mode"] == "pr"
    assert pr["push_images"] is False
    assert pr["image_path"] == "ghcr.io/o/untrusted-pr/example"
    assert pr["description"] == "UNREVIEWED pr build of example. Not for production use."
    dev = manifest_action.compute(fixture_manifest(), event_name="push", ref_name="feature", repository="o/docker-example", sha=SHA, pr_number="", fork=False, repo_dir=FIXTURE)
    assert dev["mode"] == "dev"
    assert dev["image_path"] == "ghcr.io/o/dev/example"
    assert dev["description"] == "UNREVIEWED dev build of example. Not for production use."


def test_plan_other_tags_and_sonarr_example():
    examples = ROOT.parent / "builder" / "examples" / "sonarr" / "image.yml"
    if not examples.is_file():
        pytest.skip("builder examples not available")
    m = rv.load_manifest(examples)
    out = manifest_action.compute(m, event_name="push", ref_name="master", repository="o/docker-sonarr", sha=SHA, pr_number="", fork=False, repo_dir=examples.parent)
    assert out["other_tags"] == "develop"
    assert out["version_arg"] == "SONARR_VERSION"
    assert out["description"] == "Sonarr (formerly NZBdrone) is a PVR for usenet and bittorrent users."
    assert out["base_image"] == ""  # no Dockerfile beside the example manifest


def test_first_sentence_strips_links_and_quotes():
    assert manifest_action.first_sentence('[Foo](https://x) is "great". More.') == "Foo is 'great'."


def test_manifest_cli_writes_outputs(tmp_path, monkeypatch, capsys):
    out_file = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    rc = manifest_action.main(["--manifest", str(FIXTURE / "image.yml"), "--event-name", "push", "--ref-name", "main", "--repository", "o/docker-example", "--sha", SHA, "--work-dir", str(tmp_path / "work")])
    assert rc == 0
    text = out_file.read_text()
    assert "mode<<" in text and "\nlive\n" in text
    assert (tmp_path / "work" / "manifest.json").is_file()
    assert "manifest_path<<" in text
