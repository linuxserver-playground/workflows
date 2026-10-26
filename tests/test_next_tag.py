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
    # The rebuild republishes ls2, so its record diffs against the release before it.
    assert out["previous_tag"] == "4.0.20.3014-ls2" and out["diff_base"] != "4.0.20.3014-ls2"


def test_diff_base_is_the_previous_release_for_a_new_commit():
    out = next_tag.pick(releases(), "latest", "master", other_tags=["develop"])
    assert out["diff_base"] == out["previous_tag"]


def test_no_releases_starts_at_one():
    out = next_tag.pick([], "latest", "master")
    assert out == {
        "ls_build": 1,
        "previous_tag": "",
        "previous_ls_build": 0,
        "previous_release_url": "",
        "head_already_released": False,
        "diff_base": "",
    }


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
    rc = next_tag.main(["--repo", "o/r", "--branch", "master", "--release-tag", "latest", "--other-tags", "latest,develop", "--head-tags", "", "--releases-json", str(FIXTURE), "--no-siblings"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["ls_build"] == 3


def test_tag_pattern_shapes():
    assert next_tag.matches_branch("v1.2.3-ls10", "latest", []).group("n") == "10"
    assert next_tag.matches_branch("develop-1.2.3-ls1", "latest", ["develop"]) is None
    assert next_tag.matches_branch("develop-1.2.3-ls1", "develop", []).group("upstream") == "1.2.3"
    assert next_tag.matches_branch("1.2.3", "latest", []) is None


def test_package_change_commit_moves_head_so_the_follow_up_build_gets_n_plus_one():
    """A package check that finds changes commits 'Update package versions' and never releases. The
    push starts a normal build on the new HEAD, which no release tag points at, so it is N+1 even
    though the previous HEAD had already been released as N."""
    rels = releases()
    # The weekly check runs on the released commit: HEAD carries 4.0.20.3014-ls2, so a release from
    # here would be ls2 again (a rebuild). The check does not release.
    check = next_tag.pick(rels, "latest", "master", other_tags=["develop"], head_tags=["4.0.20.3014-ls2"])
    assert check["ls_build"] == 2 and check["head_already_released"] is True
    # The follow-up build runs on the package commit, which has no tag.
    follow_up = next_tag.pick(rels, "latest", "master", other_tags=["develop"], head_tags=[])
    assert follow_up["ls_build"] == 3 and follow_up["previous_tag"] == "4.0.20.3014-ls2"
    assert follow_up["head_already_released"] is False


def fake_gh(files: dict[str, str | None]):
    """gh api as live_sibling_tags calls it: the branch listing, then image.yml per branch."""
    calls: list[str] = []

    def gh(path: str, *args: str) -> str | None:
        calls.append(path)
        if "/branches" in path:
            return "\n".join(files) + "\n"
        ref = path.split("?ref=")[1]
        return files.get(ref)

    return gh, calls


def test_live_sibling_tags_reads_each_branchs_own_manifest(monkeypatch):
    gh, calls = fake_gh({
        "master": "release: {branch: master, tag: latest}\n",
        "romm": "release: {branch: romm, tag: romm}\n",  # live, not yet in master's release.tags
        "borigin": "release: {branch: borigin, tag: origin}\n",  # the tag is not the branch name
        "feature/x": "release: {branch: master, tag: latest}\n",  # a dev branch: its image.yml names master
        "docs": None,  # no image.yml
        "broken": "release: [\n",
    })
    monkeypatch.setattr(next_tag, "gh", gh)
    assert next_tag.live_sibling_tags("o/docker-webstation", "master") == ["romm", "origin"]
    assert "repos/o/docker-webstation/contents/image.yml?ref=feature%2Fx" in calls
    assert "repos/o/docker-webstation/contents/image.yml?ref=master" not in calls


def test_unadvertised_branch_releases_are_not_claimed_by_latest(monkeypatch, tmp_path, capsys):
    """webstation's romm released romm-1.0-ls1 before master's release.tags listed romm."""
    gh, _ = fake_gh({"master": "release: {branch: master, tag: latest}\n", "romm": "release: {branch: romm, tag: romm}\n"})
    monkeypatch.setattr(next_tag, "gh", gh)
    releases = tmp_path / "releases.json"
    releases.write_text(json.dumps([
        {"tag_name": "romm-1.0-ls1", "target_commitish": "a" * 40},
        {"tag_name": "2.5.0-ls7", "target_commitish": "b" * 40},
    ]))
    args = ["--repo", "o/docker-webstation", "--branch", "master", "--release-tag", "latest", "--other-tags", "latest", "--head-tags", "", "--releases-json", str(releases)]
    assert next_tag.main(args) == 0
    assert json.loads(capsys.readouterr().out)["previous_tag"] == "2.5.0-ls7"
    # Without the siblings master would have taken romm's release for its own.
    assert next_tag.main([*args, "--no-siblings"]) == 0
    assert json.loads(capsys.readouterr().out)["previous_tag"] == "romm-1.0-ls1"


def test_prefixed_branches_do_not_look_for_siblings(monkeypatch, capsys):
    monkeypatch.setattr(next_tag, "gh", lambda *a: (_ for _ in ()).throw(AssertionError("no gh call")))
    args = ["--repo", "o/r", "--branch", "develop", "--release-tag", "develop", "--head-tags", "", "--releases-json", str(FIXTURE)]
    assert next_tag.main(args) == 0


def test_branch_listing_failure_falls_back_to_release_tags(monkeypatch, capsys):
    monkeypatch.setattr(next_tag, "gh", lambda *a: None)
    assert next_tag.live_sibling_tags("o/r", "master") == []
    assert "could not list branches" in capsys.readouterr().err
