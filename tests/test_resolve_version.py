"""resolve_version: every version_source.type with mocked HTTP, plus the post-processing rules."""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

import resolve_version as rv


class FakeHttp:
    """Maps URL (or URL prefix) -> (status, body). Records every request."""

    def __init__(self, routes: dict[str, tuple[int, bytes | str | dict | list]]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str]] = []

    def __call__(self, url: str, headers=None, method: str = "GET", timeout: int = 60) -> rv.HttpResponse:
        self.calls.append((method, url))
        for prefix, (status, body) in self.routes.items():
            if url == prefix or url.startswith(prefix):
                if isinstance(body, (dict, list)):
                    body = json.dumps(body).encode()
                elif isinstance(body, str):
                    body = body.encode()
                return rv.HttpResponse(status, body, {}, url)
        return rv.HttpResponse(404, b"not found", {}, url)


@pytest.fixture
def http(monkeypatch):
    def install(routes):
        fake = FakeHttp(routes)
        monkeypatch.setattr(rv, "http_get", fake)
        return fake

    return install


def manifest(version: dict, platforms=("amd64", "arm64")) -> dict:
    return {"upstream": {"version": version}, "build": {"platforms": list(platforms)}}


# -------------------------------------------------------------------------------------------- github


def test_github_release_stable_reads_latest(http):
    fake = http(
        {
            "https://api.github.com/repos/Sonarr/Sonarr/releases/latest": (
                200,
                {"tag_name": "v4.0.20.3014", "html_url": "https://github.com/Sonarr/Sonarr/releases/tag/v4.0.20.3014"},
            )
        }
    )
    out = rv.resolve(manifest({"type": "github_release", "repo": "Sonarr/Sonarr"}))
    assert out == {
        "upstream": "v4.0.20.3014",
        "upstream_clean": "v4.0.20.3014",
        "semver": "4.0.20",
        "source": "github_release",
        "source_url": "https://github.com/Sonarr/Sonarr/releases/tag/v4.0.20.3014",
    }
    assert fake.calls == [("GET", "https://api.github.com/repos/Sonarr/Sonarr/releases/latest")]


def test_github_release_stable_without_release_raises(http):
    http({"https://api.github.com/repos/o/r/releases/latest": (404, {"message": "Not Found"})})
    with pytest.raises(rv.ResolveError, match="no stable release"):
        rv.resolve(manifest({"type": "github_release", "repo": "o/r"}))


def test_github_release_filtered_list_is_newest_published_first(http):
    # rpcs3-binaries-linux: every release tags one commit, so GitHub lists them by name, not by date.
    http(
        {
            "https://api.github.com/repos/o/r/releases": (
                200,
                [
                    {"tag_name": "build-fff0", "draft": False, "prerelease": False, "published_at": "2021-09-24T18:37:11Z"},
                    {"tag_name": "build-6c44", "draft": False, "prerelease": False, "published_at": "2026-10-28T09:39:44Z"},
                    {"tag_name": "build-0000", "draft": True, "prerelease": False, "published_at": "2026-10-29T00:00:00Z"},
                    {"tag_name": "desktop-1", "draft": False, "prerelease": False, "published_at": "2026-10-28T10:00:00Z"},
                ],
            )
        }
    )
    out = rv.resolve(manifest({"type": "github_release", "repo": "o/r", "exclude": "^desktop"}))
    assert out["upstream"] == "build-6c44"
    out = rv.resolve(manifest({"type": "github_release", "repo": "o/r", "include": "^build-"}))
    assert out["upstream"] == "build-6c44"


def test_github_release_sort_version(http):
    http(
        {
            "https://api.github.com/repos/nextcloud/server/releases": (
                200,
                [
                    {"tag_name": "v34.0.5", "prerelease": False, "published_at": "2026-10-27T00:00:00Z"},
                    {"tag_name": "v35.0.2", "prerelease": False, "published_at": "2026-10-20T00:00:00Z"},
                    {"tag_name": "v35.0.10", "prerelease": False, "published_at": "2026-10-01T00:00:00Z"},
                    {"tag_name": "v35.0.11rc1", "prerelease": True, "published_at": "2026-10-28T00:00:00Z"},
                ],
            )
        }
    )
    out = rv.resolve(manifest({"type": "github_release", "repo": "nextcloud/server", "sort": "version"}))
    assert out["upstream"] == "v35.0.10"
    source = {"type": "github_release", "repo": "nextcloud/server", "prerelease": True, "include": "rc", "sort": "version"}
    assert rv.resolve(manifest(source))["upstream"] == "v35.0.11rc1"


def test_version_key_orders_like_sort_v():
    versions = ["35.0.2rc2", "4.10", "35.0.10", "35.0.2", "4.9.1"]
    assert sorted(versions, key=rv.version_key) == ["4.9.1", "4.10", "35.0.2", "35.0.2rc2", "35.0.10"]


def test_github_release_prerelease_and_exclude(http):
    http(
        {
            "https://api.github.com/repos/o/r/releases": (
                200,
                [
                    {"tag_name": "nightly-20261001", "draft": False, "prerelease": True},
                    {"tag_name": "v2.0.0-rc1", "draft": False, "prerelease": True},
                    {"tag_name": "v1.9.0", "draft": False, "prerelease": False},
                ],
            )
        }
    )
    out = rv.resolve(manifest({"type": "github_release", "repo": "o/r", "prerelease": True, "exclude": "^nightly"}))
    assert out["upstream"] == "v2.0.0-rc1"
    assert out["source_url"] == "https://github.com/o/r/releases/tag/v2.0.0-rc1"


def test_github_release_none_matching_raises(http):
    http({"https://api.github.com/repos/o/r/releases": (200, [{"tag_name": "v1", "draft": True}])})
    with pytest.raises(rv.ResolveError):
        rv.resolve(manifest({"type": "github_release", "repo": "o/r", "exclude": "^nightly"}))


def test_github_token_header(http, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "abc")
    assert rv.github_headers()["Authorization"] == "Bearer abc"
    monkeypatch.delenv("GITHUB_TOKEN")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert "Authorization" not in rv.github_headers()


def test_github_commit(http):
    sha = "0123456789abcdef0123456789abcdef01234567"
    http({"https://api.github.com/repos/o/r/commits/main": (200, {"sha": sha})})
    out = rv.resolve(manifest({"type": "github_commit", "repo": "o/r", "branch": "main"}))
    assert out["upstream"] == "01234567"
    assert out["semver"] is None
    assert out["source_url"] == f"https://github.com/o/r/commit/{sha}"


def test_gitlab_commit(http):
    http(
        {
            "https://gitlab.com/api/v4/projects/123/repository/commits/master": (
                200,
                {"id": "fedcba9876543210fedcba9876543210fedcba98", "web_url": "https://gitlab.com/g/p/-/commit/fedcba98"},
            )
        }
    )
    out = rv.resolve(manifest({"type": "gitlab_commit", "project_id": 123, "branch": "master"}))
    assert out["upstream"] == "fedcba98"
    assert out["source_url"] == "https://gitlab.com/g/p/-/commit/fedcba98"


def test_codeberg_release_prefers_stable(http):
    http(
        {
            "https://codeberg.org/api/v1/repos/forgejo/forgejo/releases": (
                200,
                [{"tag_name": "v9.0.0-rc", "prerelease": True}, {"tag_name": "v8.0.3", "prerelease": False, "html_url": "https://codeberg.org/forgejo/forgejo/releases/tag/v8.0.3"}],
            )
        }
    )
    out = rv.resolve(manifest({"type": "codeberg_release", "repo": "forgejo/forgejo"}))
    assert out["upstream"] == "v8.0.3"
    assert out["semver"] == "8.0.3"


def test_gitea_release_falls_back_to_prerelease(http):
    http({"https://git.example.org/api/v1/repos/a/b/releases": (200, [{"tag_name": "1.2.0-beta", "prerelease": True}])})
    out = rv.resolve(manifest({"type": "gitea_release", "base_url": "https://git.example.org/", "repo": "a/b"}))
    assert out["upstream"] == "1.2.0-beta"
    assert out["source_url"] == "https://git.example.org/a/b/releases/tag/1.2.0-beta"


# -------------------------------------------------------------------------------------------- feeds


def test_json_with_jq(http):
    body = [{"releaseChannel": "v4-stable", "version": "4.0.20.3014"}, {"releaseChannel": "v5-develop", "version": "5.0.1"}]
    http({"https://services.sonarr.tv/v1/releases": (200, body)})
    out = rv.resolve(manifest({"type": "json", "url": "https://services.sonarr.tv/v1/releases", "jq": '.[] | select(.releaseChannel=="v4-stable") | .version'}))
    assert out["upstream"] == "4.0.20.3014"
    assert out["source_url"] == "https://services.sonarr.tv/v1/releases"


def test_json_jq_no_output_raises(http):
    http({"https://x/": (200, {"a": 1})})
    with pytest.raises(rv.ResolveError):
        rv.resolve(manifest({"type": "json", "url": "https://x/", "jq": ".missing[]?"}))


def test_json_sort_version_takes_the_highest_line(http):
    http({"https://projects.blender.org/api/v1/repos/blender/blender/tags": (200, [{"name": "v5.2.3"}, {"name": "v5.10.0"}, {"name": "v4.5.9"}])})
    source = {"type": "json", "url": "https://projects.blender.org/api/v1/repos/blender/blender/tags", "jq": ".[].name", "sort": "version"}
    assert rv.resolve(manifest(source))["upstream"] == "v5.10.0"


def test_regex_first_match_group(http):
    http({"https://flathub/metainfo.xml": (200, '<release version="2609a" date="x"/>\n<release version="2603" date="y"/>')})
    source = {"type": "regex", "url": "https://flathub/metainfo.xml", "pattern": '<release version="([^"]+)"'}
    out = rv.resolve(manifest(source))
    assert out["upstream"] == "2609a"
    assert out["source_url"] == "https://flathub/metainfo.xml"


def test_regex_sort_version_and_named_group(http):
    page = '<a href="./8.9.0/">8.9.0/</a> <a href="./9.1.0/">9.1.0/</a> <a href="./9.0.0/">9.0.0/</a>'
    http({"https://mirrors/digikam/": (200, page)})
    source = {"type": "regex", "url": "https://mirrors/digikam/", "pattern": 'href="\\./(?P<version>[0-9.]+)/"', "sort": "version"}
    assert rv.resolve(manifest(source))["upstream"] == "9.1.0"


def test_regex_without_match_raises(http):
    http({"https://x/": (200, "nothing here")})
    with pytest.raises(rv.ResolveError, match="matched nothing"):
        rv.resolve(manifest({"type": "regex", "url": "https://x/", "pattern": "v([0-9.]+)"}))


def test_regex_search_url_reads_the_redirect(monkeypatch):
    calls = []

    def fake(url, headers=None, method="GET", timeout=60):
        calls.append((method, url))
        return rv.HttpResponse(200, b"", {}, "https://vaemendis.net/ubooquity/downloads/Ubooquity-3.1.0.zip")

    monkeypatch.setattr(rv, "http_get", fake)
    source = {"type": "regex", "url": "http://vaemendis.net/ubooquity/service/download.php", "pattern": "Ubooquity-([0-9.]+)\\.zip", "search": "url"}
    assert rv.resolve(manifest(source))["upstream"] == "3.1.0"
    assert calls == [("HEAD", "http://vaemendis.net/ubooquity/service/download.php")]


ARCH = {"results": [{"pkgname": "darktable", "pkgver": "5.6.2", "pkgrel": "1", "epoch": 2}]}


def test_arch_package(http):
    fake = http({"https://archlinux.org/packages/search/json/": (200, ARCH)})
    out = rv.resolve(manifest({"type": "arch_package", "package": "darktable"}))
    assert out["upstream"] == "5.6.2"
    assert out["source_url"] == "https://archlinux.org/packages/extra/x86_64/darktable/"
    assert fake.calls == [("GET", "https://archlinux.org/packages/search/json/?name=darktable&repo=Extra&arch=x86_64")]
    assert rv.resolve(manifest({"type": "arch_package", "package": "darktable", "release": True}))["upstream"] == "5.6.2-1"


def test_arch_package_missing_raises(http):
    http({"https://archlinux.org/packages/search/json/": (200, {"results": []})})
    with pytest.raises(rv.ResolveError, match="not in extra"):
        rv.resolve(manifest({"type": "arch_package", "package": "nope"}))


def test_static():
    out = rv.resolve(manifest({"type": "static", "version": "7.2.7"}))
    assert out["upstream"] == "7.2.7"
    assert out["source_url"] == "none"


def test_npm(http):
    http({"https://registry.npmjs.org/@scope%2Fpkg": (200, {"dist-tags": {"latest": "3.4.5", "next": "4.0.0-0"}})})
    assert rv.resolve(manifest({"type": "npm", "package": "@scope/pkg"}))["upstream"] == "3.4.5"
    assert rv.resolve(manifest({"type": "npm", "package": "@scope/pkg", "dist_tag": "next"}))["upstream"] == "4.0.0-0"
    with pytest.raises(rv.ResolveError):
        rv.resolve(manifest({"type": "npm", "package": "@scope/pkg", "dist_tag": "nope"}))


def test_pip(http):
    http({"https://pypi.org/pypi/certbot/json": (200, {"info": {"version": "5.2.1"}})})
    out = rv.resolve(manifest({"type": "pip", "package": "certbot"}))
    assert out["upstream"] == "5.2.1"
    assert out["source_url"] == "https://pypi.org/project/certbot/"


def apkindex_tgz(packages: dict[str, str]) -> bytes:
    text = "".join(f"C:Q1abc\nP:{p}\nV:{v}\nA:x86_64\n\n" for p, v in packages.items())
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("APKINDEX")
        info.size = len(text.encode())
        tar.addfile(info, io.BytesIO(text.encode()))
    return buf.getvalue()


def test_alpine_apk_requires_agreement(http):
    fake = http(
        {
            "http://dl-cdn.alpinelinux.org/alpine/v3.22/community/x86_64/APKINDEX.tar.gz": (200, apkindex_tgz({"nginx": "1.26.3-r0", "other": "1"})),
            "http://dl-cdn.alpinelinux.org/alpine/v3.22/community/aarch64/APKINDEX.tar.gz": (200, apkindex_tgz({"nginx": "1.26.3-r0"})),
        }
    )
    out = rv.resolve(manifest({"type": "alpine_apk", "repo_url": "http://dl-cdn.alpinelinux.org/alpine/v3.22/community/", "package": "nginx"}))
    assert out["upstream"] == "1.26.3-r0"
    assert len(fake.calls) == 2


def test_alpine_apk_disagreement_is_not_ready(http):
    http(
        {
            "https://r/x86_64/APKINDEX.tar.gz": (200, apkindex_tgz({"nginx": "1.26.3-r0"})),
            "https://r/aarch64/APKINDEX.tar.gz": (200, apkindex_tgz({"nginx": "1.26.2-r0"})),
        }
    )
    with pytest.raises(rv.AssetsNotReady):
        rv.resolve(manifest({"type": "alpine_apk", "repo_url": "https://r", "package": "nginx"}))


def test_alpine_apk_single_platform_only_fetches_one(http):
    fake = http({"https://r/x86_64/APKINDEX.tar.gz": (200, apkindex_tgz({"nginx": "1.26.3-r0"}))})
    out = rv.resolve(manifest({"type": "alpine_apk", "repo_url": "https://r", "package": "nginx"}, platforms=("amd64",)))
    assert out["upstream_clean"] == "1.26.3-r0"
    assert len(fake.calls) == 1


PACKAGES = """Package: foo
Version: 1:2.0-1

Package: bar
Version: 1.10.0-1ubuntu1

Package: bar
Version: 1.9.5-2

Package: bar
Version: 1.10.0~rc1-1
"""


def test_deb_packages_picks_highest_version(http):
    http({"https://deb.example.org/dists/x/main/binary-amd64/Packages.gz": (200, gzip.compress(PACKAGES.encode()))})
    out = rv.resolve(manifest({"type": "deb_packages", "index_url": "https://deb.example.org/dists/x/main/binary-amd64/Packages.gz", "package": "bar"}))
    assert out["upstream"] == "1.10.0-1ubuntu1"
    assert out["upstream_clean"] == "1.10.0-1ubuntu1"
    assert out["semver"] == "1.10.0"


def test_deb_version_ordering():
    versions = ["1.10.0~rc1-1", "1.9.5-2", "1.10.0-1ubuntu1", "1:0.1-1", "1.10.0-1"]
    assert sorted(versions, key=rv.deb_version_key) == ["1.9.5-2", "1.10.0~rc1-1", "1.10.0-1", "1.10.0-1ubuntu1", "1:0.1-1"]


def test_deb_repo_runs_docker(monkeypatch):
    calls = []

    class P:
        returncode = 0
        stdout = "deadbeef\n"
        stderr = ""

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return P()

    monkeypatch.setattr(rv.subprocess, "run", fake_run)
    out = rv.resolve(manifest({"type": "deb_repo", "image": "ubuntu:noble", "sources": "deb http://x noble main", "packages": "a b"}))
    assert out["upstream"] == "deadbeef"
    assert calls[0][:4] == ["docker", "run", "--rm", "ubuntu:noble"]
    assert "apt-cache --no-all-versions show a b" in calls[0][-1]


def test_blob_md5(http):
    body = b"hello world"
    http({"https://files.example.org/latest.tar.gz": (200, body)})
    out = rv.resolve(manifest({"type": "blob", "url": "https://files.example.org/latest.tar.gz"}))
    assert out["upstream"] == hashlib.md5(body).hexdigest()[:8]


def test_blob_404_raises(http):
    http({})
    with pytest.raises(rv.ResolveError):
        rv.resolve(manifest({"type": "blob", "url": "https://files.example.org/missing"}))


def test_os_uses_package_versions_md5(tmp_path: Path):
    (tmp_path / "package_versions.txt").write_text("NAME VERSION\nbash 5.2\n")
    expected = hashlib.md5((tmp_path / "package_versions.txt").read_bytes()).hexdigest()[:8]
    out = rv.resolve(manifest({"type": "os"}), manifest_dir=tmp_path)
    assert out == {"upstream": expected, "upstream_clean": expected, "semver": None, "source": "os", "source_url": "none"}


def test_os_without_package_file(tmp_path: Path):
    assert rv.resolve(manifest({"type": "os"}), manifest_dir=tmp_path)["upstream"] == "none"


def test_command_source():
    out = rv.resolve(manifest({"type": "command", "run": "echo ignored; printf 'v1.2.3\\n'"}))
    assert out["upstream"] == "v1.2.3"
    assert out["semver"] == "1.2.3"
    with pytest.raises(rv.ResolveError):
        rv.resolve(manifest({"type": "command", "run": "exit 3"}))


def test_command_source_survives_sigpipe():
    # `yes | head -1` exits 141 under pipefail once head closes the pipe; the line printed is the version.
    assert rv.resolve(manifest({"type": "command", "run": "yes 2.0.1 | head -n 1"}))["upstream"] == "2.0.1"

def test_command_source_survives_curl_write_error():
    # curl ignores SIGPIPE and exits 23 when grep -m 1 closes the pipe (webgrabplus).
    assert rv.resolve(manifest({"type": "command", "run": "(printf '5.3.0\\n'; exit 23) | head -n 1"}))["upstream"] == "5.3.0"


def test_command_runner_is_replaceable_and_times_out(monkeypatch):
    seen = []

    def runner(script):
        seen.append(script)
        return rv.subprocess.CompletedProcess(["bash"], 0, "1.0.0\n", "")

    monkeypatch.setattr(rv, "run_command", runner)
    assert rv.resolve(manifest({"type": "command", "run": "echo hi"}))["upstream"] == "1.0.0"
    assert seen == ["echo hi"]

    def slow(script):
        raise rv.subprocess.TimeoutExpired(["bash"], 120)

    monkeypatch.setattr(rv, "run_command", slow)
    with pytest.raises(rv.ResolveError, match="timed out"):
        rv.resolve(manifest({"type": "command", "run": "sleep 999"}))


def test_combined_joins_its_parts_after_their_own_transforms(http):
    http(
        {
            "https://api.github.com/repos/moby/moby/releases": (
                200,
                [
                    {"tag_name": "api/v1.52.0", "draft": False, "prerelease": False},
                    {"tag_name": "docker-v29.9.2", "draft": False, "prerelease": False},
                ],
            ),
            "https://api.github.com/repos/docker/compose/releases/latest": (200, {"tag_name": "v5.6.1", "html_url": "c"}),
        }
    )
    source = {
        "type": "combined",
        "sources": [
            {
                "type": "github_release",
                "repo": "moby/moby",
                "exclude": "^(?!docker-)",
                "transform": [{"regex": "^docker-v", "replace": ""}],
            },
            {"type": "github_release", "repo": "docker/compose", "transform": [{"regex": "^v", "replace": ""}]},
        ],
    }
    out = rv.resolve(manifest(source))
    assert out["upstream"] == "29.9.2-5.6.1"
    assert out["source"] == "combined"
    assert out["source_url"] == "https://github.com/moby/moby/releases/tag/docker-v29.9.2"
    joined = rv.resolve(manifest({**source, "separator": "_", "transform": [{"regex": "_", "replace": "+"}]}))
    assert joined["upstream"] == "29.9.2+5.6.1" and joined["upstream_clean"] == "29.9.25.6.1"


def test_combined_fails_when_a_part_is_empty(http):
    http({"https://api.github.com/repos/o/r/commits/main": (200, {"sha": ""})})
    source = {"type": "combined", "sources": [{"type": "github_commit", "repo": "o/r", "branch": "main"}]}
    with pytest.raises(rv.ResolveError, match="combined part github_commit"):
        rv.resolve(manifest(source))


def test_unknown_type():
    with pytest.raises(rv.ResolveError):
        rv.resolve(manifest({"type": "carrier_pigeon"}))


# -------------------------------------------------------------------------------------------- post-processing


def test_transforms_then_sanitize(http):
    http({"https://pypi.org/pypi/x/json": (200, {"info": {"version": "1.2.3+local"}})})
    out = rv.resolve(manifest({"type": "pip", "package": "x", "transform": [{"regex": r"\+local$", "replace": "~rc"}]}))
    assert out["upstream"] == "1.2.3~rc"
    assert out["upstream_clean"] == "1.2.3rc"


def test_sanitize_removes_contract_characters():
    assert rv.sanitize("1~2,3%4@5+6;7:8/9 10") == "12345678910"


def test_semver_rules():
    today = dt.date(2026, 10, 24)
    assert rv.derive_semver("v4.0.20.3014", today) == "4.0.20"
    assert rv.derive_semver("1.2", today) == "1.2.20261024"
    assert rv.derive_semver("1.2-beta", today) is None
    assert rv.derive_semver("abcdef12", today) is None
    assert rv.derive_semver("2024.08", today) == "2024.8.20261024"


def test_require_assets_head(http):
    fake = http(
        {
            "https://pypi.org/pypi/x/json": (200, {"info": {"version": "2.0"}}),
            "https://dl.example.org/x-2.0-amd64.tar.gz": (200, b""),
            "https://dl.example.org/x-2.0-arm64.tar.gz": (200, b""),
        }
    )
    src = {"type": "pip", "package": "x", "require_assets": ["https://dl.example.org/x-${VERSION}-amd64.tar.gz", "https://dl.example.org/x-${VERSION}-arm64.tar.gz"]}
    rv.resolve(manifest(src))
    assert ("HEAD", "https://dl.example.org/x-2.0-arm64.tar.gz") in fake.calls


def test_require_assets_missing_is_not_ready(http):
    http({"https://pypi.org/pypi/x/json": (200, {"info": {"version": "2.0"}})})
    with pytest.raises(rv.AssetsNotReady):
        rv.resolve(manifest({"type": "pip", "package": "x", "require_assets": ["https://dl.example.org/${VERSION}.zip"]}))


def test_empty_or_null_version_raises(http):
    http({"https://pypi.org/pypi/x/json": (200, {"info": {"version": "null"}})})
    with pytest.raises(rv.ResolveError):
        rv.resolve(manifest({"type": "pip", "package": "x"}))


# -------------------------------------------------------------------------------------------- CLI


def test_cli_reads_yaml_and_json(tmp_path: Path, http, capsys):
    http({"https://pypi.org/pypi/certbot/json": (200, {"info": {"version": "5.2.1"}})})
    yml = tmp_path / "image.yml"
    yml.write_text("schema: 1\nname: swag\nupstream:\n  version:\n    type: pip\n    package: certbot\nbuild:\n  platforms: [amd64]\n")
    assert rv.main([str(yml)]) == 0
    assert json.loads(capsys.readouterr().out)["upstream"] == "5.2.1"
    js = tmp_path / "image.json"
    js.write_text(json.dumps({"upstream": {"version": {"type": "pip", "package": "certbot"}}, "build": {"platforms": ["amd64"]}}))
    assert rv.main([str(js)]) == 0


def test_cli_exit_codes(tmp_path: Path, http):
    http({})
    js = tmp_path / "image.json"
    js.write_text(json.dumps({"upstream": {"version": {"type": "pip", "package": "x", "require_assets": ["https://a/${VERSION}"]}}, "build": {"platforms": ["amd64"]}}))
    assert rv.main([str(js)]) == 1  # pypi 404 -> ResolveError
    http({"https://pypi.org/pypi/x/json": (200, {"info": {"version": "1"}})})
    assert rv.main([str(js)]) == 3  # asset 404 -> AssetsNotReady
    assert rv.main([]) == 2


def test_examples_from_builder_repo_parse():
    """The converted manifests must be readable by the module (no network: only load)."""
    examples = Path(__file__).resolve().parents[2] / "builder" / "examples"
    if not examples.is_dir():
        pytest.skip("builder examples not checked out next to this repo")
    schema = json.loads((examples.parent / "schemas" / "image.schema.json").read_text())
    known = set(schema["$defs"]["version_source"]["properties"]["type"]["enum"])
    found = set()
    for path in examples.glob("*/image.yml"):
        m = rv.load_manifest(path)
        assert m["upstream"]["version"]["type"] in known
        found.add(m["upstream"]["version"]["type"])
    assert found


def test_os_reads_the_package_table_from_the_workspace(tmp_path, monkeypatch):
    """The manifest lives in a temp dir during the build; the package table is in the checkout."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package_versions.txt").write_text("pkg 1.0\n")
    manifest_dir = tmp_path / "tmp"
    manifest_dir.mkdir()
    monkeypatch.setenv("GITHUB_WORKSPACE", str(repo))
    version, _ = rv._os(manifest_dir)
    assert version == hashlib.md5(b"pkg 1.0\n").hexdigest()[:8]
    monkeypatch.delenv("GITHUB_WORKSPACE")
    assert rv._os(manifest_dir) == ("none", "none")
