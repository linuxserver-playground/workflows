"""next_tag: build number selection from GitHub Release fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import next_tag

FIXTURE = Path(__file__).parent / "fixtures" / "releases.json"


def releases() -> list[dict]:
    return json.loads(FIXTURE.read_text())


def test_default_branch_increments_from_newest_unprefixed_release():
    out = next_tag.pick(releases(), "latest", "master", other_tags=["latest", "develop"])
    assert out["previous_tag"] == "4.0.20.3014-ls2"
    assert out["previous_ls_build"] == 2
    assert out["ls_build"] == 3
    assert out["previous_release_url"].endswith("/4.0.20.3014-ls2")


def test_prefixed_branch_only_sees_its_own_tags():
    out = next_tag.pick(releases(), "develop", "develop", other_tags=["latest", "develop"])
    assert out["previous_tag"] == "develop-5.0.1.1234-ls7"
    assert out["ls_build"] == 8


def test_head_already_tagged_keeps_number():
    out = next_tag.pick(releases(), "latest", "master", other_tags=["develop"], head_tags=["4.0.20.3014-ls2"])
    assert out["ls_build"] == 2
    assert out["head_already_released"] is True


def test_no_releases_starts_at_one():
    out = next_tag.pick([], "latest", "master")
    assert out == {"ls_build": 1, "previous_tag": "", "previous_ls_build": 0, "previous_release_url": "", "head_already_released": False}


def test_other_branch_tags_not_claimed_without_target_commitish():
    data = [
        {"tag_name": "develop-9.9.9-ls40", "target_commitish": "0" * 40, "draft": False, "html_url": "u"},
        {"tag_name": "1.0.0-ls4", "target_commitish": "1" * 40, "draft": False, "html_url": "v"},
    ]
    assert next_tag.pick(data, "latest", "master", other_tags=["develop"])["previous_tag"] == "1.0.0-ls4"
    assert next_tag.pick(data, "develop", "develop", other_tags=["latest"])["previous_tag"] == "develop-9.9.9-ls40"


def test_drafts_and_foreign_branches_ignored():
    data = [
        {"tag_name": "2.0.0-ls9", "target_commitish": "master", "draft": True, "html_url": ""},
        {"tag_name": "2.0.0-ls8", "target_commitish": "nightly", "draft": False, "html_url": ""},
        {"tag_name": "2.0.0-ls7", "target_commitish": "master", "draft": False, "html_url": ""},
    ]
    assert next_tag.pick(data, "latest", "master")["ls_build"] == 8


def test_cli_with_fixture_file(capsys):
    rc = next_tag.main(["--repo", "o/r", "--branch", "master", "--release-tag", "latest", "--other-tags", "latest,develop", "--head-tags", "", "--releases-json", str(FIXTURE)])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["ls_build"] == 3


def test_tag_pattern_shapes():
    assert next_tag.matches_branch("v1.2.3-ls10", "latest", []).group("n") == "10"
    assert next_tag.matches_branch("develop-1.2.3-ls1", "latest", ["develop"]) is None
    assert next_tag.matches_branch("develop-1.2.3-ls1", "develop", []).group("upstream") == "1.2.3"
    assert next_tag.matches_branch("1.2.3", "latest", []) is None
