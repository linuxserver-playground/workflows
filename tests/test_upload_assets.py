"""upload_assets: a Release ends up holding the bundle, through the failures GitHub's asset API showed on 2026-11-04."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import upload_assets
from upload_assets import CallFailed


class FakeRelease:
    """A Release's assets, with failures to inject. ``lag`` keeps a new upload out of the listing for that many
    listings, as GitHub's eventually consistent listing did."""

    def __init__(self, assets: dict[str, bytes] | None = None) -> None:
        self.next_id = 100
        self.stored: dict[int, dict[str, Any]] = {}
        self.hidden: dict[int, int] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_delete: dict[str, int] = {}
        self.fail_upload: dict[str, int] = {}
        self.fail_list = 0
        self.lag = 0
        self.upload_lost_after_store: dict[str, int] = {}
        for name, data in (assets or {}).items():
            self._store(name, data)

    def _store(self, name: str, data: bytes, state: str = "uploaded") -> int:
        self.next_id += 1
        digest = "sha256:" + __import__("hashlib").sha256(data).hexdigest()
        self.stored[self.next_id] = {"id": self.next_id, "name": name, "state": state, "digest": digest}
        return self.next_id

    def assets(self) -> list[dict[str, Any]]:
        self.calls.append(("list", ""))
        if self.fail_list:
            self.fail_list -= 1
            raise CallFailed("gh api repos/o/r/releases/tags/t: HTTP 404: Not Found")
        out = []
        for asset_id, asset in self.stored.items():
            if self.hidden.get(asset_id, 0) > 0:
                self.hidden[asset_id] -= 1
                continue
            out.append(dict(asset))
        return out

    def delete(self, asset_id: int) -> None:
        name = self.stored[asset_id]["name"]
        self.calls.append(("delete", name))
        if self.fail_delete.get(name, 0):
            self.fail_delete[name] -= 1
            raise CallFailed("gh api --method DELETE: HTTP 500")
        del self.stored[asset_id]

    def upload(self, path: Path) -> None:
        self.calls.append(("upload", path.name))
        if any(a["name"] == path.name for a in self.stored.values()):
            raise CallFailed("HTTP 422: Validation Failed\nReleaseAsset.name already exists")
        if self.upload_lost_after_store.get(path.name, 0):
            # The upload went through but its answer was lost.
            self.upload_lost_after_store[path.name] -= 1
            self._lagged(path)
            raise CallFailed("HTTP 502: Bad Gateway")
        if self.fail_upload.get(path.name, 0):
            self.fail_upload[path.name] -= 1
            raise CallFailed("HTTP 404: Not Found (https://uploads.github.com/...)")
        self._lagged(path)

    def _lagged(self, path: Path) -> None:
        asset_id = self._store(path.name, path.read_bytes())
        if self.lag:
            self.hidden[asset_id] = self.lag

    def holds(self, files: list[Path]) -> bool:
        return not upload_assets.outstanding(
            {p.name: (p, upload_assets.sha256(p)) for p in files}, list(self.stored.values())
        )


@pytest.fixture
def bundle(tmp_path: Path) -> list[Path]:
    files = []
    for name, body in (("build.json", b'{"v": 2}'), ("record.json", b"{}"), ("files.amd64.json.gz", b"\x1f\x8b new")):
        p = tmp_path / name
        p.write_bytes(body)
        files.append(p)
    return files


def run(release: FakeRelease, files: list[Path], rounds: int = 8) -> list[str]:
    return upload_assets.attach(release, files, rounds=rounds, sleep=lambda _s: None)


def test_new_release_gets_every_file(bundle):
    release = FakeRelease()
    assert run(release, bundle) == []
    assert release.holds(bundle)


def test_a_rerun_of_the_same_bundle_deletes_nothing(bundle):
    release = FakeRelease({p.name: p.read_bytes() for p in bundle})
    assert run(release, bundle) == []
    assert [c for c in release.calls if c[0] != "list"] == []


def test_a_rebuild_replaces_only_the_files_that_changed(bundle):
    old = {p.name: p.read_bytes() for p in bundle}
    old["build.json"] = b'{"v": 1}'
    release = FakeRelease(old)
    assert run(release, bundle) == []
    assert [c for c in release.calls if c[0] != "list"] == [("delete", "build.json"), ("upload", "build.json")]
    assert release.holds(bundle)


def test_failing_deletes_keep_the_old_asset_until_one_succeeds(bundle):
    """heimdall v2.8.4-ls7: every delete answered 500 for minutes."""
    release = FakeRelease({p.name: b"old" for p in bundle})
    release.fail_delete = {"build.json": 4}
    assert run(release, bundle) == []
    assert release.holds(bundle)


def test_deletes_that_never_succeed_leave_the_old_asset_and_fail(bundle):
    release = FakeRelease({p.name: b"old" for p in bundle})
    release.fail_delete = {"build.json": 99}
    assert run(release, bundle, rounds=3) == ["build.json"]
    assert any(a["name"] == "build.json" for a in release.stored.values())


def test_a_new_release_that_answers_404_for_a_moment(bundle):
    """python ha-alpine324-3.14.10-ls3: the upload to a release made seconds before answered 404."""
    release = FakeRelease()
    release.fail_list = 1
    release.fail_upload = {"record.json": 2}
    assert run(release, bundle) == []
    assert release.holds(bundle)


def test_a_lost_answer_and_a_lagging_listing_do_not_loop_on_already_exists(bundle):
    """heimdall attempt 1: the retry found a name it had not expected (422 already exists)."""
    release = FakeRelease()
    release.lag = 1
    release.upload_lost_after_store = {"files.amd64.json.gz": 1}
    assert run(release, bundle) == []
    assert release.holds(bundle)
    assert sum(1 for a in release.stored.values() if a["name"] == "files.amd64.json.gz") == 1


def test_a_half_uploaded_asset_is_replaced(bundle):
    release = FakeRelease()
    release._store("build.json", bundle[0].read_bytes(), state="starter")
    assert run(release, bundle) == []
    assert release.holds(bundle)


def test_cli_rejects_a_missing_file(tmp_path, capsys):
    assert upload_assets.main(["--repo", "o/r", "--tag", "t", str(tmp_path / "nope.json")]) == 2
    assert "no such file" in capsys.readouterr().err
