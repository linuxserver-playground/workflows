"""mod_version: the version and floating tags of a mod build, per version line of mod.yml."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import mod_version as mv
import resolve_version as rv

GO = "https://go.dev/dl/?mode=json"
RELEASES = [{"version": "go1.27.3", "stable": True}, {"version": "go1.26.10", "stable": True}]
ALL = [{"version": "go1.28rc1", "stable": False}, *RELEASES]


def go(url: str, jq: str) -> dict:
    return {"type": "json", "url": url, "jq": jq, "transform": [{"regex": "^go", "replace": ""}]}


def golang() -> dict:
    return {
        "name": "golang",
        "target": "code-server",
        "platforms": ["amd64"],
        "upstream": {
            "version": go(GO, ".[0].version"),
            "tags": ["major", "minor"],
            "lines": [
                {"name": "previous", "version": go(GO, ".[1].version"), "tags": ["minor"]},
                {"name": "unstable", "version": go(GO + "&include=all", "[.[] | select(.stable | not)][0].version")},
            ],
        },
    }


@pytest.fixture
def goweb(monkeypatch):
    def fake(url, headers=None, method="GET", timeout=60):
        body = ALL if url.endswith("include=all") else RELEASES
        return rv.HttpResponse(200, json.dumps(body).encode(), {}, url)

    monkeypatch.setattr(rv, "http_get", fake)


def test_each_line_resolves_its_own_version_and_tags(goweb) -> None:
    mod = golang()
    assert mv.plan(mod) == {"mod_version": "1.27.3", "version_tag": "1.27.3", "line": "", "aliases": ["1", "1.27"]}
    assert mv.plan(mod, line="previous")["aliases"] == ["1.26"]
    assert mv.plan(mod, line="previous")["mod_version"] == "1.26.10"
    assert mv.plan(mod, line="unstable") == {
        "mod_version": "1.28rc1",
        "version_tag": "1.28rc1",
        "line": "unstable",
        "aliases": [],
    }


def test_an_input_version_is_built_as_given_with_its_lines_tags(goweb) -> None:
    assert mv.plan(golang(), version="1.26.9", line="previous")["aliases"] == ["1.26"]
    assert mv.plan(golang(), version="1.27.2")["aliases"] == ["1", "1.27"]
    with pytest.raises(KeyError):
        mv.plan(golang(), line="oldstable")


def test_mods_without_lines_or_upstream_keep_their_old_plan(goweb) -> None:
    mod = golang()
    del mod["upstream"]["lines"], mod["upstream"]["tags"]
    assert mv.plan(mod) == {"mod_version": "1.27.3", "version_tag": "1.27.3", "line": "", "aliases": []}
    assert mv.plan({"name": "tshoot"}) == {"mod_version": "", "version_tag": "", "line": "", "aliases": []}
    assert mv.plan({"name": "x"}, version="a b+c")["version_tag"] == "abc"


def test_unresolved_source_warns_and_builds_without_a_version(monkeypatch, capsys) -> None:
    monkeypatch.setattr(rv, "http_get", lambda url, **_: rv.HttpResponse(500, b"down", {}, url))
    assert mv.plan(golang())["mod_version"] == ""
    assert "::warning file=mod.yml::" in capsys.readouterr().out


def test_main_appends_github_output(goweb, tmp_path: Path) -> None:
    (tmp_path / "mod.yml").write_text(yaml.safe_dump(golang()), encoding="utf-8")
    out = tmp_path / "out"
    assert mv.main([str(tmp_path / "mod.yml"), "--line", "previous", "--output", str(out)]) == 0
    assert out.read_text() == 'mod_version=1.26.10\nversion_tag=1.26.10\nline=previous\naliases=["1.26"]\n'
    assert mv.main([str(tmp_path / "mod.yml"), "--line", "nope", "--output", str(out)]) == 2
