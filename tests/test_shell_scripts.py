"""The bash scripts, run for real with a stubbed docker on PATH and bare git remotes in tmp_path:
publish.sh (the imagetools command list from staging sources), update_generated.sh (the [skip ci]
subject) and package_check.sh (compare, commit, rebase, push)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INDEX = "sha256:" + "d" * 64

# The stub answers every docker call publish.sh and update_generated.sh make and logs the arguments.
DOCKER_STUB = f'''#!/usr/bin/env bash
echo "$*" >> "${{DOCKER_LOG}}"
if [[ "$1 $2 $3" == "buildx imagetools inspect" ]]; then
  echo '"{INDEX}"'
elif [[ "$1" == "run" ]]; then
  # The builder container's render: change a generated file in the mounted repo (our cwd).
  for a in "$@"; do [[ "$a" == "render" ]] && echo "rendered" >> README.md; done
fi
exit 0
'''

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True, env={**os.environ, **GIT_ENV}).stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(DOCKER_STUB)
    (bin_dir / "docker").chmod(0o755)
    log = tmp_path / "docker.log"
    log.write_text("")
    e = {
        **os.environ,
        **GIT_ENV,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "DOCKER_LOG": str(log),
        "GITHUB_OUTPUT": str(tmp_path / "gh_output"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "RUNNER_TEMP": str(tmp_path / "runner_temp"),
        "RETRY_BASE_DELAY": "0",
    }
    (tmp_path / "runner_temp").mkdir()
    return e


def outputs(env: dict) -> dict[str, str]:
    text = Path(env["GITHUB_OUTPUT"]).read_text() if Path(env["GITHUB_OUTPUT"]).exists() else ""
    out: dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        name, delim = lines[i].split("<<", 1)
        j = lines.index(delim, i + 1)
        out[name] = "\n".join(lines[i + 1 : j])
        i = j + 1
    return out


def docker_calls(env: dict) -> list[str]:
    return [line for line in Path(env["DOCKER_LOG"]).read_text().splitlines() if line]


def run(script: Path, env: dict, *args: str, cwd: Path | None = None, extra: dict | None = None) -> subprocess.CompletedProcess:
    e = {**env, "GITHUB_ACTION_PATH": str(script.parent), **(extra or {})}
    return subprocess.run(["bash", str(script), *args], cwd=cwd or ROOT, env=e, capture_output=True, text=True, check=False)


# -------------------------------------------------------------------------------------------- publish.sh

PUBLISH = ROOT / ".github" / "actions" / "publish-manifest" / "publish.sh"
IMAGE = "ghcr.io/o/example"
STAGING = {"amd64": "ghcr.io/o/build/example:amd64-0123456-99", "arm64": "ghcr.io/o/build/example:arm64-0123456-99"}


def test_publish_creates_every_tag_from_the_staging_references(env):
    proc = run(PUBLISH, env, extra={"IMAGE_PATH": IMAGE, "PRIMARY_TAG": "1.2.3-ls4", "TAGS": json.dumps(["latest", "1.2.3-ls4", "version-1.2.3"]), "PLATFORMS": json.dumps(["amd64", "arm64"]), "SOURCES": json.dumps(STAGING), "DESCRIPTION": "Example image"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = docker_calls(env)
    ann = "--annotation index:org.opencontainers.image.description=Example image"
    assert calls == [
        f"buildx imagetools create {ann} -t {IMAGE}:latest -t {IMAGE}:1.2.3-ls4 -t {IMAGE}:version-1.2.3 {STAGING['amd64']} {STAGING['arm64']}",
        f"buildx imagetools create {ann} -t {IMAGE}:amd64-latest -t {IMAGE}:amd64-1.2.3-ls4 -t {IMAGE}:amd64-version-1.2.3 {STAGING['amd64']}",
        f"buildx imagetools create {ann} -t {IMAGE}:arm64v8-latest -t {IMAGE}:arm64v8-1.2.3-ls4 -t {IMAGE}:arm64v8-version-1.2.3 {STAGING['arm64']}",
        f"buildx imagetools inspect {IMAGE}:1.2.3-ls4 --format {{{{json .Manifest.Digest}}}}",
    ]
    # Nothing under the staging namespace is ever a target.
    assert not any(f"-t ghcr.io/o/build/" in c for c in calls)
    out = outputs(env)
    assert out["index_digest"] == INDEX
    assert f"{IMAGE}:arm64v8-1.2.3-ls4" in json.loads(out["published"])
    assert STAGING["amd64"] in Path(env["GITHUB_STEP_SUMMARY"]).read_text()


def test_publish_without_sources_keeps_the_legacy_layout(env):
    proc = run(PUBLISH, env, extra={"IMAGE_PATH": IMAGE, "PRIMARY_TAG": "1.2.3-ls4", "TAGS": json.dumps(["latest", "1.2.3-ls4"]), "PLATFORMS": json.dumps(["amd64"])})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert docker_calls(env)[:2] == [
        f"buildx imagetools create -t {IMAGE}:latest -t {IMAGE}:1.2.3-ls4 {IMAGE}:amd64-1.2.3-ls4",
        f"buildx imagetools create -t {IMAGE}:amd64-latest {IMAGE}:amd64-1.2.3-ls4",
    ]


def test_publish_refuses_a_platform_without_staging_reference(env):
    proc = run(PUBLISH, env, extra={"IMAGE_PATH": IMAGE, "PRIMARY_TAG": "t", "TAGS": '["t"]', "PLATFORMS": '["amd64","arm64"]', "SOURCES": json.dumps({"amd64": STAGING["amd64"]})})
    assert proc.returncode != 0
    assert "no staging reference for arm64" in proc.stdout
    assert docker_calls(env) == []


# -------------------------------------------------------------------------------------------- git fixtures


def make_remote(tmp_path: Path, files: dict[str, str]) -> tuple[Path, Path]:
    """A bare origin with one commit on main, and a clone of it."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(origin)], check=True, env={**os.environ, **GIT_ENV})
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(clone)], check=True, env={**os.environ, **GIT_ENV}, capture_output=True)
    git("checkout", "--quiet", "-b", "main", cwd=clone)
    for name, text in files.items():
        (clone / name).parent.mkdir(parents=True, exist_ok=True)
        (clone / name).write_text(text)
    git("add", "-A", cwd=clone)
    git("commit", "--quiet", "-m", "Initial", cwd=clone)
    git("push", "--quiet", "-u", "origin", "main", cwd=clone)
    return origin, clone


def other_clone(origin: Path, tmp_path: Path, name: str) -> Path:
    other = tmp_path / name
    subprocess.run(["git", "clone", "--quiet", str(origin), str(other)], check=True, env={**os.environ, **GIT_ENV}, capture_output=True)
    return other


# -------------------------------------------------------------------------------------------- update_generated.sh

UPDATE_GENERATED = ROOT / "scripts" / "update_generated.sh"


def test_generated_files_commit_carries_skip_ci(env, tmp_path):
    origin, clone = make_remote(tmp_path, {"README.md": "# x\n", "AGENTS.md": "a\n", "LICENSE": "l\n", ".github/workflows/build.yml": "on: push\n"})
    before = git("rev-parse", "HEAD", cwd=clone)
    proc = run(UPDATE_GENERATED, env, "render", "ghcr.io/o/builder:latest", "main", cwd=clone)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    subject = git("log", "-1", "--format=%s", cwd=clone)
    assert subject == "Update generated files [skip ci]"
    assert git("rev-parse", "main", cwd=origin) == git("rev-parse", "HEAD", cwd=clone) != before
    out = outputs(env)
    assert out["generated_updated"] == "true" and out["checkout_sha"] == git("rev-parse", "HEAD", cwd=clone)


def test_generated_files_current_makes_no_commit(env, tmp_path):
    _, clone = make_remote(tmp_path, {"README.md": "# x\n"})
    # A docker stub whose render changes nothing.
    stub = Path(env["PATH"].split(":")[0]) / "docker"
    stub.write_text('#!/usr/bin/env bash\necho "$*" >> "${DOCKER_LOG}"\nexit 0\n')
    proc = run(UPDATE_GENERATED, env, "render", "ghcr.io/o/builder:latest", "main", cwd=clone)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert git("log", "-1", "--format=%s", cwd=clone) == "Initial"
    assert outputs(env)["generated_updated"] == "false"


def check_stub(env: dict, output: str, rc: int = 0) -> None:
    """A docker stub whose builder container prints `output` and exits `rc`."""
    stub = Path(env["PATH"].split(":")[0]) / "docker"
    stub.write_text(f'#!/usr/bin/env bash\necho "$*" >> "${{DOCKER_LOG}}"\nif [[ "$1" == "run" ]]; then printf "%s\\n" "{output}"; exit {rc}; fi\nexit 0\n')


def test_check_warns_on_drift_and_passes(env, tmp_path):
    _, clone = make_remote(tmp_path, {"README.md": "# x\n", "image.yml": "name: x\n"})
    check_stub(env, "warning: README.md: <file>: differs (run builder render)")
    proc = run(UPDATE_GENERATED, env, "check", "ghcr.io/o/builder:latest", cwd=clone)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "check --drift warn" in Path(env["DOCKER_LOG"]).read_text()
    assert "::warning::generated files differ from the current builder (README.md)" in proc.stdout
    assert outputs(env)["checkout_sha"] == git("rev-parse", "HEAD", cwd=clone)


def test_check_fails_on_real_problems(env, tmp_path):
    _, clone = make_remote(tmp_path, {"README.md": "# x\n"})
    check_stub(env, "image.yml: name: must be a string", rc=1)
    proc = run(UPDATE_GENERATED, env, "check", "ghcr.io/o/builder:latest", cwd=clone)
    assert proc.returncode != 0
    assert "builder check failed" in proc.stdout


def test_check_fails_when_a_pull_request_edits_generated_files(env, tmp_path):
    _, clone = make_remote(tmp_path, {"README.md": "# x\n", "Dockerfile": "FROM x\n"})
    base = git("rev-parse", "HEAD", cwd=clone)
    check_stub(env, "ok")
    (clone / "Dockerfile").write_text("FROM y\n")
    git("commit", "--quiet", "-am", "Change the Dockerfile", cwd=clone)
    assert run(UPDATE_GENERATED, {**env, "BASE_SHA": base}, "check", "ghcr.io/o/builder:latest", cwd=clone).returncode == 0
    (clone / "README.md").write_text("# hand edited\n")
    git("commit", "--quiet", "-am", "Edit the README", cwd=clone)
    proc = run(UPDATE_GENERATED, {**env, "BASE_SHA": base}, "check", "ghcr.io/o/builder:latest", cwd=clone)
    assert proc.returncode != 0
    assert "this pull request edits generated files (README.md)" in proc.stdout


# -------------------------------------------------------------------------------------------- package_check.sh

PACKAGE_CHECK = ROOT / ".github" / "actions" / "package-check" / "package_check.sh"
TABLE = "NAME   VERSION  TYPE\nbash   5.2.37   apk\n"
NEW_TABLE = TABLE.replace("5.2.37", "5.2.38")


def package_check(env: dict, tmp_path: Path, clone: Path, fresh: str, **extra: str) -> subprocess.CompletedProcess:
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    (out / "package_versions.amd64.txt").write_text(fresh)
    e = {"OUT_DIR": str(out), "REPO_DIR": str(clone), "SHA": git("rev-parse", "HEAD", cwd=clone), "BRANCH": "main", "REPOSITORY": "o/docker-example", "PUSH_DELAY": "0", **extra}
    return run(PACKAGE_CHECK, env, extra=e)


def test_package_check_unchanged_commits_nothing(env, tmp_path):
    origin, clone = make_remote(tmp_path, {"package_versions.txt": TABLE})
    proc = package_check(env, tmp_path, clone, TABLE.replace("\n", "   \n"))  # trailing whitespace only
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert outputs(env)["changed"] == "false" and outputs(env)["commit_sha"] == ""
    assert git("log", "--format=%s", cwd=origin) == "Initial"
    assert "packages unchanged" in Path(env["GITHUB_STEP_SUMMARY"]).read_text()


def test_package_check_changed_commits_and_dispatches_the_release_build(env, tmp_path):
    origin, clone = make_remote(tmp_path, {"package_versions.txt": TABLE})
    extra = gh_env(env, tmp_path)
    proc = package_check(env, tmp_path, clone, NEW_TABLE, **extra)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = outputs(env)
    assert out["changed"] == "true"
    assert git("log", "-1", "--format=%s", cwd=origin) == "Update package versions"
    assert git("rev-parse", "main", cwd=origin) == out["commit_sha"]
    assert git("show", "main:package_versions.txt", cwd=origin) == NEW_TABLE.rstrip("\n")
    # A workflow-token push never triggers workflows, so the release build is dispatched explicitly.
    assert "workflow run build.yml --repo o/docker-example --ref main -f reason=rebuild" in Path(extra["GH_LOG"]).read_text()
    summary = Path(env["GITHUB_STEP_SUMMARY"]).read_text()
    assert f"committed as `{out['commit_sha']}`" in summary and "release build dispatched" in summary


def test_package_check_seeds_a_missing_file(env, tmp_path):
    origin, clone = make_remote(tmp_path, {"README.md": "# x\n"})
    proc = package_check(env, tmp_path, clone, TABLE)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert outputs(env)["changed"] == "true" and outputs(env)["committed_hash"] == "none"
    assert git("show", "main:package_versions.txt", cwd=origin) == TABLE.rstrip("\n")


def test_package_check_rebases_over_a_concurrent_commit(env, tmp_path):
    origin, clone = make_remote(tmp_path, {"package_versions.txt": TABLE, "README.md": "# x\n"})
    built_sha = git("rev-parse", "HEAD", cwd=clone)
    other = other_clone(origin, tmp_path, "other")
    (other / "README.md").write_text("# y\n")
    git("commit", "--quiet", "-am", "Edit readme", cwd=other)
    git("push", "--quiet", "origin", "main", cwd=other)
    proc = package_check(env, tmp_path, clone, NEW_TABLE)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert git("log", "--format=%s", cwd=origin).splitlines() == ["Update package versions", "Edit readme", "Initial"]
    assert git("show", "main:README.md", cwd=origin) == "# y"
    assert outputs(env)["commit_sha"] != built_sha


def test_package_check_fails_when_the_push_is_impossible(env, tmp_path):
    origin, clone = make_remote(tmp_path, {"package_versions.txt": TABLE})
    shutil.rmtree(origin)  # the remote is gone: fetch and push fail every time
    proc = package_check(env, tmp_path, clone, NEW_TABLE, PUSH_ATTEMPTS="2")
    assert proc.returncode != 0
    assert "could not push Update package versions" in proc.stdout


# -------------------------------------------------------------------------------------------- create_release.sh

CREATE_RELEASE = ROOT / ".github" / "actions" / "release" / "create_release.sh"
SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"

# gh stub: logs every call; the tag lookup answers from GH_TAG_LOOKUP (empty = 404).
GH_STUB = '''#!/usr/bin/env bash
echo "$*" >> "${GH_LOG}"
if [[ "$1" == "api" && "$2" == repos/*/git/ref/tags/* ]]; then
  [[ -n "${GH_TAG_LOOKUP:-}" ]] || exit 1
  echo "${GH_TAG_LOOKUP}"
elif [[ "$1" == "api" && "$2" == repos/*/git/tags/* ]]; then
  echo "${GH_TAG_OBJECT_SHA:-}"
fi
exit 0
'''


def gh_env(env: dict, tmp_path: Path, **extra: str) -> dict:
    stub_dir = Path(env["PATH"].split(":")[0])
    (stub_dir / "gh").write_text(GH_STUB)
    (stub_dir / "gh").chmod(0o755)
    log = tmp_path / "gh.log"
    log.write_text("")
    return {"GH_LOG": str(log), **extra}


def test_release_tags_the_sha_and_targets_the_branch(env, tmp_path):
    notes = tmp_path / "body.md"
    notes.write_text("notes")
    # The tag is pushed with git to the repository (here a local bare remote), not through the refs API.
    origin, _clone = make_remote(tmp_path, {"README.md": "# x\n"})
    sha = git("rev-parse", "HEAD", cwd=origin)
    extra = gh_env(env, tmp_path, RELEASE_GIT_REMOTE=str(origin))
    proc = run(CREATE_RELEASE, env, "4.0.20.3014-ls3", sha, "master", "o/docker-sonarr", str(notes), "false", extra=extra)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert git("rev-parse", "refs/tags/4.0.20.3014-ls3", cwd=origin) == sha
    assert Path(extra["GH_LOG"]).read_text().splitlines() == [
        "api repos/o/docker-sonarr/git/ref/tags/4.0.20.3014-ls3 --jq .object.type + \" \" + .object.sha",
        f"release create 4.0.20.3014-ls3 --repo o/docker-sonarr --target master --title 4.0.20.3014-ls3 --notes-file {notes}",
    ]
    # Prerelease flag, and the sha never appears as the release target.
    extra = gh_env(env, tmp_path, RELEASE_GIT_REMOTE=str(origin))
    proc = run(CREATE_RELEASE, env, "develop-5.0.1-ls7", sha, "develop", "o/r", str(notes), "true", extra=extra)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    last = Path(extra["GH_LOG"]).read_text().splitlines()[-1]
    assert last.endswith(f"--target develop --title develop-5.0.1-ls7 --notes-file {notes} --prerelease")
    assert f"--target {SHA}" not in Path(extra["GH_LOG"]).read_text()


def test_release_accepts_an_existing_tag_at_the_same_commit_only(env, tmp_path):
    notes = tmp_path / "body.md"
    notes.write_text("notes")
    # Lightweight tag already at the sha (a re-run): no tag creation, release still on the branch.
    extra = gh_env(env, tmp_path, GH_TAG_LOOKUP=f"commit {SHA}")
    proc = run(CREATE_RELEASE, env, "t-ls1", SHA, "main", "o/r", str(notes), extra=extra)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = Path(extra["GH_LOG"]).read_text().splitlines()
    assert len(calls) == 2 and "git/refs" not in calls[1] and "--target main" in calls[1]
    # Annotated tag resolving to the same commit is fine too.
    extra = gh_env(env, tmp_path, GH_TAG_LOOKUP=f"tag {OTHER_SHA}", GH_TAG_OBJECT_SHA=SHA)
    assert run(CREATE_RELEASE, env, "t-ls1", SHA, "main", "o/r", str(notes), extra=extra).returncode == 0
    # A tag at another commit must never be moved or released over.
    extra = gh_env(env, tmp_path, GH_TAG_LOOKUP=f"commit {OTHER_SHA}")
    proc = run(CREATE_RELEASE, env, "t-ls1", SHA, "main", "o/r", str(notes), extra=extra)
    assert proc.returncode != 0 and "never moves" in proc.stdout
    assert "release create" not in Path(extra["GH_LOG"]).read_text()


def _retry(env: dict, tmp_path: Path, error: str, also: str | None) -> tuple[int, int]:
    """retry_ghcr over a command that fails once with `error`: (exit code, attempts made)."""
    count = tmp_path / "attempts"
    count.write_text("")
    flaky = f'echo x >> {count}; [[ $(wc -l < {count}) -gt 1 ]] || {{ echo "{error}" >&2; exit 1; }}'
    run_env = {**env, **({"RETRY_ALSO": also} if also else {})}
    proc = subprocess.run(["bash", "-c", f'source "{ROOT}/scripts/lib.sh"; retry_ghcr bash -c \'{flaky}\''], env=run_env, capture_output=True, text=True)
    return proc.returncode, len(count.read_text().splitlines())


def test_retry_ghcr_retries_rate_limits_and_returns_other_errors_at_once(env, tmp_path):
    assert _retry(env, tmp_path, "toomanyrequests: slow down", None) == (0, 2)
    assert _retry(env, tmp_path, "Error response from daemon: manifest unknown", None) == (1, 1)


def test_retry_ghcr_retries_the_extra_pattern_it_is_given(env, tmp_path):
    assert _retry(env, tmp_path, "Error response from daemon: manifest unknown", "manifest unknown") == (0, 2)
