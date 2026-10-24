"""package_versions (the reason=packages comparison) and cleanup_staging (the build/* age filter)."""

from __future__ import annotations

import datetime as dt
import hashlib
import json

import cleanup_staging
import package_versions as pv

TABLE = "NAME        VERSION   TYPE\nbash        5.2.37    apk\njq          1.7.1     apk\n"


def test_normalise_ignores_trailing_whitespace_and_line_endings():
    assert pv.normalise("a  \r\nb\t\n\n\n") == "a\nb\n"
    assert pv.normalise("") == ""
    assert pv.digest(TABLE) == pv.digest(TABLE.replace("\n", "   \r\n") + "\n\n")
    assert pv.digest(TABLE) == hashlib.md5(TABLE.encode()).hexdigest()[:8]


def test_compare_unchanged_changed_missing():
    same = pv.compare(TABLE, TABLE.replace("\n", " \n"))
    assert same["changed"] is False and same["status"] == "same" and same["fresh"] == same["committed"]
    changed = pv.compare(TABLE.replace("1.7.1", "1.8.0"), TABLE)
    assert changed["changed"] is True and changed["status"] == "different" and changed["fresh"] != changed["committed"]
    missing = pv.compare(TABLE, None)
    assert missing == {"changed": True, "status": "missing", "fresh": pv.digest(TABLE), "committed": "none"}


def test_compare_cli_reports_arm64_against_previous_release(tmp_path, monkeypatch, capsys):
    fresh = tmp_path / "package_versions.amd64.txt"
    fresh.write_text(TABLE)
    committed = tmp_path / "package_versions.txt"
    committed.write_text(TABLE)
    fresh_arm = tmp_path / "package_versions.arm64.txt"
    fresh_arm.write_text(TABLE.replace("5.2.37", "5.2.38"))
    seen = {}

    def fake_fetch(url, timeout=30):
        seen["url"] = url
        return TABLE

    monkeypatch.setattr(pv, "fetch", fake_fetch)
    url = "https://github.com/o/docker-x/releases/download/1.2.3-ls4/package_versions.arm64.txt"
    assert pv.main(["compare", "--fresh", str(fresh), "--committed", str(committed), "--fresh-arm64", str(fresh_arm), "--previous-arm64-url", url]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["changed"] is False and out["status"] == "same"
    assert out["arm64"]["changed"] is True and seen["url"] == url
    # The committed file is absent: changed, and the first run seeds it.
    assert pv.main(["compare", "--fresh", str(fresh), "--committed", str(tmp_path / "nope.txt")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "missing" and out["changed"] is True and out["arm64"] is None
    # An unreachable previous asset is reported, never fatal.
    monkeypatch.setattr(pv, "fetch", lambda url, timeout=30: None)
    assert pv.main(["compare", "--fresh", str(fresh), "--committed", str(committed), "--fresh-arm64", str(fresh_arm), "--previous-arm64-url", url]) == 0
    assert json.loads(capsys.readouterr().out)["arm64"]["status"] == "unavailable"


# -------------------------------------------------------------------------------------------- cleanup-staging


def test_cleanup_staging_age_filter_and_package_selection():
    now = dt.datetime(2026, 10, 24, 12, 0, tzinfo=dt.UTC)
    versions = [
        {"id": 1, "updated_at": "2026-10-10T00:00:00Z", "metadata": {"container": {"tags": ["amd64-abc1234-1"]}}},
        {"id": 2, "updated_at": "2026-10-17T11:59:00Z", "metadata": {"container": {"tags": ["arm64-abc1234-1"]}}},  # 7 days + 1 minute
        {"id": 3, "updated_at": "2026-10-17T12:01:00Z", "metadata": {"container": {"tags": []}}},  # just inside
        {"id": 4, "created_at": "2026-09-01T00:00:00Z", "metadata": {"container": {"tags": ["x"]}}},  # falls back to created_at
        {"id": 5, "metadata": {"container": {"tags": ["no-timestamp"]}}},  # kept
        {"id": 6, "updated_at": "2026-10-24T11:00:00Z", "metadata": {"container": {"tags": ["amd64-def5678-9"]}}},
    ]
    assert [v["id"] for v in cleanup_staging.select_old_versions(versions, now, 7)] == [1, 2, 4]
    assert [v["id"] for v in cleanup_staging.select_old_versions(versions, now, 0)] == [1, 2, 3, 4, 6]
    packages = [{"name": "sonarr"}, {"name": "build/sonarr"}, {"name": "untrusted-pr/sonarr"}, {"name": "build/swag"}, {"name": "dev/swag"}]
    assert cleanup_staging.staging_packages(packages) == ["build/sonarr", "build/swag"]


def test_cleanup_staging_cli_dry_run(monkeypatch, capsys):
    calls = []

    def fake_api(args):
        calls.append(args)
        if args[-1].startswith("orgs/o/packages?"):
            return [[{"name": "build/sonarr"}, {"name": "sonarr"}]]
        if "/versions?" in args[-1]:
            return [[{"id": 11, "updated_at": "2000-01-01T00:00:00Z", "metadata": {"container": {"tags": ["amd64-aaaaaaa-1"]}}}, {"id": 12, "updated_at": dt.datetime.now(tz=dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "metadata": {"container": {"tags": []}}}]]
        raise AssertionError(args)

    monkeypatch.setattr(cleanup_staging, "gh_api", fake_api)
    assert cleanup_staging.main(["--org", "o", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would delete build/sonarr version 11" in out and "version 12" not in out
    assert not any("DELETE" in a for a in calls)
