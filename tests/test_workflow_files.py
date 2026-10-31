"""Static checks over the workflow and action YAML: parse, pins, namespaces, actionlint."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
ACTIONS = sorted((ROOT / ".github" / "actions").glob("*/action.yml"))
TEMPLATES = sorted((ROOT / "templates").glob("*.yml"))
ALL_YAML = WORKFLOWS + ACTIONS + TEMPLATES

EXPECTED_ACTIONS = {"free-disk", "manifest", "resolve-version", "next-tag", "build-arch", "test-arch", "publish-manifest", "emit-bundle", "record", "release", "notify-control", "package-check", "pages", "artifact-download", "artifact-upload"}
CLOSED_PR = "github.event_name == 'pull_request' && github.event.action == 'closed'"
NOT_PAGES = "inputs.reason != 'pages'"
ALLOWED_REGISTRIES = re.compile(r"ghcr\.io/(linuxserver-playground/|linuxserver/baseimage-)")
FORBIDDEN = re.compile(r"docker\.io|hub\.docker\.com|registry\.gitlab\.com|quay\.io|s3://|scarf|discord", re.IGNORECASE)


def load(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.mark.parametrize("path", ALL_YAML, ids=lambda p: str(p.relative_to(ROOT)))
def test_yaml_parses(path: Path):
    data = load(path)
    assert isinstance(data, dict)
    if path in WORKFLOWS or path in TEMPLATES:
        assert "jobs" in data and (True in data or "on" in data)  # PyYAML reads 'on' as True
    else:
        assert data["runs"]["using"] == "composite"


def test_all_expected_actions_exist():
    assert {p.parent.name for p in ACTIONS} == EXPECTED_ACTIONS


def test_image_build_contract():
    wf = load(ROOT / ".github" / "workflows" / "image-build.yml")
    inputs = wf[True]["workflow_call"]["inputs"]
    assert inputs["reason"]["default"] == "push"
    assert inputs["workflows_ref"]["default"] == "v1"
    assert inputs["ci_image"]["default"] == "ghcr.io/linuxserver-playground/ci:latest"
    assert set(wf[True]["workflow_call"]["secrets"]) == {"CONTROL_URL", "CONTROL_EVENTS_SECRET", "ATTACHMENTS_TOKEN"}
    assert all(not v.get("required") for v in wf[True]["workflow_call"]["secrets"].values())
    assert list(wf["jobs"]) == ["prepare", "arch", "publish", "pages"]
    arch = wf["jobs"]["arch"]
    assert arch["strategy"]["fail-fast"] is False
    assert arch["runs-on"] == "${{ matrix.runner }}"
    publish = wf["jobs"]["publish"]
    assert publish["if"] == "${{ always() && " + NOT_PAGES + " && !(" + CLOSED_PR + ") }}"
    assert publish["permissions"] == {"contents": "write", "packages": "write", "pull-requests": "write", "id-token": "write", "attestations": "write", "pages": "write"}
    used = {s.get("uses", "") for job in wf["jobs"].values() for s in job["steps"]}
    for a in EXPECTED_ACTIONS:
        assert any(u.endswith(f"/.github/actions/{a}") for u in used), a


def _steps(wf: dict, job: str) -> dict:
    return {s.get("id") or s["name"]: s for s in wf["jobs"][job]["steps"]}


def test_arch_jobs_push_to_staging_and_publish_creates_from_it():
    wf = load(ROOT / ".github" / "workflows" / "image-build.yml")
    prepare = wf["jobs"]["prepare"]
    assert prepare["outputs"]["staging_refs"] == "${{ steps.tags.outputs.staging_refs }}"
    assert "--run-id" in _steps(wf, "prepare")["tags"]["run"]
    build = _steps(wf, "arch")["build"]
    assert build["with"]["staging_ref"] == "${{ fromJSON(needs.prepare.outputs.staging_refs)[matrix.arch] }}"
    publish = _steps(wf, "publish")
    assert publish["manifest"]["with"]["sources"] == "${{ needs.prepare.outputs.staging_refs }}"
    # Nothing consumer-visible is written before the gate said so: a green build that pushes images and is not a
    # package check that found no change.
    for step in ("manifest", "Attest provenance (live)", "release"):
        assert "steps.gate.outputs.release == 'true'" in publish[step]["if"], step
    gate = publish["gate"]["run"]
    assert 'if [[ "${PACKAGE_CHECK}" == "true" && "${CHANGED}" != "true" ]]; then skip=true; fi' in gate
    assert 'if [[ "${RESULT}" == "success" && "${PUSH}" == "true" && "${skip}" == "false" ]]; then release=true; fi' in gate
    assert publish["gate"]["env"]["CHANGED"] == "${{ steps.compare.outputs.changed }}"


def test_package_check_is_one_build_that_releases_only_on_a_change():
    """reason=packages compares before publishing and releases from the same run; every live release commits
    the table afterwards with [skip ci]. No dispatch, no second build (Jenkins' Update-packages had one)."""
    wf = load(ROOT / ".github" / "workflows" / "image-build.yml")
    assert wf["jobs"]["prepare"]["outputs"]["package_check"] == "${{ inputs.reason == 'packages' && steps.manifest.outputs.mode == 'live' && steps.manifest.outputs.push_images == 'true' }}"
    # A package check that finds changes gets a new -lsN even though HEAD is the released commit plus its table commit.
    assert _steps(wf, "prepare")["next"]["with"]["fresh"] == "${{ inputs.reason == 'packages' }}"
    publish = _steps(wf, "publish")
    assert publish["compare"]["if"] == "steps.result.outputs.result == 'success' && env.PACKAGE_CHECK == 'true'"
    assert publish["compare"]["with"]["mode"] == "compare" and publish["compare"]["with"]["sha"] == "${{ needs.prepare.outputs.checkout_sha }}"
    assert publish["repo"]["with"]["persist-credentials"] is True
    assert publish["repo"]["if"] == "steps.result.outputs.result == 'success' && env.PUSH == 'true' && env.MODE == 'live'"
    table = publish["table"]
    assert table["if"] == "steps.release.outcome == 'success'" and table["continue-on-error"] is True
    assert table["with"]["mode"] == "commit" and table["with"]["branch"] == "${{ needs.prepare.outputs.release_branch }}"
    text = (ROOT / ".github" / "workflows" / "image-build.yml").read_text()
    assert "gh workflow run" not in text and "actions: write" not in text
    # The control plane and the summary see the folded outcome; an unchanged check reports skipped.
    assert 'result="skipped"' in publish["outcome"]["run"]
    # Any failed step of the job itself (a bundle download, say) makes the summary a failure of "publish".
    assert publish["outcome"]["env"]["JOB_STATUS"] == "${{ job.status }}"
    assert '"${JOB_STATUS}" != "success"' in publish["outcome"]["run"]
    notify = publish["Notify control plane"]["with"]
    assert notify["result"] == "${{ steps.outcome.outputs.result }}"
    assert "steps.outcome.outputs.result != 'skipped'" in publish["Fail if any job failed"]["if"]


def test_pull_requests_are_reported_by_the_trusted_half():
    build = load(ROOT / ".github" / "workflows" / "image-build.yml")
    for job in ("prepare", "arch"):
        assert build["jobs"][job]["if"] == "${{ " + NOT_PAGES + " && !(" + CLOSED_PR + ") }}"
    # The build of a pull request never posts to it and never deletes images: that is pr-report.yml.
    assert "cleanup" not in build["jobs"]
    assert "Comment on pull request" not in _steps(build, "publish")
    assert "pull_request_target" not in (ROOT / ".github" / "workflows" / "image-build.yml").read_text()
    tpl = load(ROOT / "templates" / "build.yml")
    assert tpl[True]["pull_request"] == {"types": ["opened", "synchronize", "reopened"]}

    report = load(ROOT / ".github" / "workflows" / "pr-report.yml")
    text = (ROOT / ".github" / "workflows" / "pr-report.yml").read_text()
    caller = load(ROOT / "templates" / "pr-report.yml")
    assert caller[True] == {"workflow_run": {"workflows": ["Build"], "types": ["completed"]}, "pull_request_target": {"types": ["labeled", "synchronize", "closed"]}}
    assert caller["permissions"] == {} and caller["jobs"]["report"]["secrets"] == "inherit"
    # Trusted by construction: the only checkouts are the shared workflows and, for cleanup, the base commit.
    for job_name, job in report["jobs"].items():
        for step in job["steps"]:
            if "actions/checkout" in str(step.get("uses", "")):
                ref = step["with"]["ref"]
                assert ref in ("${{ inputs.workflows_ref }}", "${{ github.event.pull_request.base.sha }}"), (job_name, ref)
    assert "head.sha }}" not in text.replace("github.event.pull_request.head.sha }}", "")  # only the concurrency group
    assert "github.event.pull_request.head.ref" not in text and "github.event.pull_request.title" not in text
    cleanup = report["jobs"]["cleanup"]
    assert cleanup["if"] == "github.event_name == 'pull_request_target' && github.event.action == 'closed'"
    assert "pr_cleanup.py" in _steps(report, "cleanup")["Delete package versions"]["run"]
    steps = _steps(report, "report")
    assert steps["publish"]["if"] == "steps.pr.outputs.publish == 'true'"
    assert steps["Post the report"]["env"]["ATTACHMENTS_TOKEN"] == "${{ secrets.ATTACHMENTS_TOKEN }}"
    assert "safe-to-publish" in report["jobs"]["unlabel"]["if"] and "synchronize" in report["jobs"]["unlabel"]["if"]


def test_cleanup_staging_workflow():
    wf = load(ROOT / ".github" / "workflows" / "cleanup-staging.yml")
    assert set(wf[True]) == {"workflow_dispatch", "schedule"}
    assert wf[True]["workflow_dispatch"]["inputs"]["max_age_days"]["default"] == "7"
    job = wf["jobs"]["cleanup"]
    assert job["env"]["PACKAGES_TOKEN"] == "${{ secrets.PACKAGES_TOKEN }}"
    steps = {s["name"]: s for s in job["steps"]}
    assert steps["No token"]["if"] == "env.PACKAGES_TOKEN == ''" and "::warning::" in steps["No token"]["run"]
    assert steps["Delete staging versions"]["if"] == "env.PACKAGES_TOKEN != ''"
    assert "cleanup_staging.py" in steps["Delete staging versions"]["run"]


def test_third_party_actions_pinned_by_major_and_listed_in_readme():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    seen = set()
    for path in WORKFLOWS + ACTIONS:
        for m in re.finditer(r"uses:\s*([\w.-]+/[\w.-]+(?:/[^@\s]+)?)@([^\s]+)", path.read_text(encoding="utf-8")):
            action, ref = m.group(1), m.group(2)
            if action.startswith("./"):
                continue
            assert re.fullmatch(r"v\d+(\.\d+)*|[0-9a-f]{40}", ref), f"{path.name}: {action}@{ref} is not pinned"
            seen.add(f"{action}@{ref}")
    assert seen, "no third-party actions found"
    for pin in seen:
        assert pin in readme, f"{pin} missing from README"


def test_no_foreign_namespaces_or_services():
    for path in list(ROOT.glob(".github/**/*")) + list((ROOT / "scripts").iterdir()) + TEMPLATES:
        if path.is_dir() or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"ghcr\.io/[\w./-]+", text):
            assert ALLOWED_REGISTRIES.match(m.group(0)), f"{path}: {m.group(0)}"
        hit = FORBIDDEN.search(text)
        assert hit is None, f"{path}: forbidden reference {hit.group(0)!r}"


def test_template_caller_file():
    tpl = load(ROOT / "templates" / "build.yml")
    assert tpl["jobs"]["build"]["uses"] == "linuxserver-playground/workflows/.github/workflows/image-build.yml@v1"
    assert tpl["jobs"]["build"]["secrets"] == "inherit"
    assert (ROOT / "templates" / "build.yml").read_text().startswith("# Generated by builder")
    assert len((ROOT / "templates" / "build.yml").read_text().splitlines()) <= 40


def test_scripts_are_executable_and_strict():
    for path in list((ROOT / "scripts").glob("*.sh")) + list((ROOT / ".github" / "actions").glob("*/*.sh")):
        assert path.stat().st_mode & 0o111, f"{path} is not executable"
        assert "set -euo pipefail" in path.read_text(encoding="utf-8") or path.name == "lib.sh", path


@pytest.mark.skipif(shutil.which("actionlint") is None, reason="actionlint not installed")
def test_actionlint():
    proc = subprocess.run(["actionlint", *map(str, WORKFLOWS)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck():
    scripts = [str(p) for p in list((ROOT / "scripts").glob("*.sh")) + list((ROOT / ".github" / "actions").glob("*/*.sh"))]
    proc = subprocess.run(["shellcheck", "-x", *scripts], capture_output=True, text=True, check=False, cwd=ROOT)
    assert proc.returncode == 0, proc.stdout


def test_fixture_manifest_valid_and_s6_executable():
    contracts = pytest.importorskip("contracts")
    data = contracts.load_yaml(ROOT / "fixtures" / "example-image" / "image.yml")
    assert contracts.errors("image", data) == []
    assert data["upstream"]["version"]["type"] == "os"
    run = ROOT / "fixtures" / "example-image" / "root" / "etc" / "s6-overlay" / "s6-rc.d" / "init-example-config" / "run"
    assert run.stat().st_mode & 0o111
    assert (ROOT / "fixtures" / "example-image" / "Dockerfile").read_text().splitlines()[2] == "FROM ghcr.io/linuxserver/baseimage-alpine:3.22"


def test_publish_derives_the_record_before_the_release():
    wf = load(ROOT / ".github" / "workflows" / "image-build.yml")
    names = [s.get("name") for s in wf["jobs"]["publish"]["steps"]]
    assert names.index("Emit build.json") < names.index("Derive record.json") < names.index("Release (live)")
    step = _steps(wf, "publish")["record"]
    assert step["uses"] == "./.lsio-workflows/.github/actions/record"
    assert step["with"]["previous_tag"] == "${{ needs.prepare.outputs.diff_base }}"
    record = load(ROOT / ".github" / "actions" / "record" / "action.yml")
    assert all(s.get("continue-on-error") for s in record["runs"]["steps"] if s.get("shell"))
    assert "python3 -m builder record" in record["runs"]["steps"][-1]["run"]
    assert '"${BUNDLE_DIR}"/record.json' in (ROOT / ".github" / "actions" / "release" / "release.sh").read_text()


def test_only_the_publish_job_writes_the_run_summary():
    """The run page shows one summary box, which notifications link: steps collect notes in LSIO_SUMMARY."""
    wf = load(ROOT / ".github" / "workflows" / "image-build.yml")
    assert wf["env"]["LSIO_SUMMARY"] == "/dev/stdout"
    assert wf["jobs"]["publish"]["env"]["LSIO_SUMMARY"].endswith("/.lsio-summary.md")
    writers = [
        (job, step.get("name"))
        for job, spec in wf["jobs"].items()
        for step in spec["steps"]
        if "GITHUB_STEP_SUMMARY" in str(step.get("run", ""))
    ]
    assert writers == [("publish", "Job summary")]
    for script in (ROOT / ".github" / "actions").glob("*/*.sh"):
        text = script.read_text(encoding="utf-8")
        assert '>> "${GITHUB_STEP_SUMMARY' not in text, script


def test_pages_deploys_after_a_live_release_and_alone_for_reason_pages():
    """CONVENTIONS §6, "Repo Pages": every live release redeploys the site; reason=pages runs only the deploy."""
    wf = load(ROOT / ".github" / "workflows" / "image-build.yml")
    publish = wf["jobs"]["publish"]
    # Only a run that can release enters the environment; the others get an empty name, which is none.
    env = publish["environment"]["name"]
    assert "needs.prepare.outputs.mode == 'live'" in env and "push_images == 'true'" in env and env.endswith("&& 'github-pages' || '' }}")
    steps = _steps(wf, "publish")
    names = [s.get("name") for s in publish["steps"]]
    # Pages before the event to control (CONVENTIONS §10): when the hub lists the build its page's files are served,
    # and the event says how the deploy went.
    assert names.index("Release (live)") < names.index("Pages (live)") < names.index("Notify control plane") < names.index("Job summary")
    notify = publish["steps"][names.index("Notify control plane")]["with"]
    assert notify["pages"] == "${{ steps.pages.outcome == 'skipped' && 'skipped' || (steps.pages.outputs.page_url != '' && 'deployed' || 'failed') }}"
    assert steps["bundle"]["with"]["published"] == "${{ steps.manifest.outputs.published }}"
    assert steps["pages"]["with"]["site_url"] == "${{ vars.SITE_URL }}"
    assert wf["jobs"]["pages"]["steps"][-1]["with"]["site_url"] == "${{ vars.SITE_URL }}"
    assert steps["pages"]["if"] == "${{ !cancelled() && steps.release.outcome == 'success' }}"
    assert steps["pages"]["continue-on-error"] is True
    assert steps["pages"]["uses"] == "./.lsio-workflows/.github/actions/pages"
    # reason=pages: one job, nothing else runs (no build, release, package commit or event to control).
    pages = wf["jobs"]["pages"]
    assert pages["if"] == "${{ inputs.reason == 'pages' }}" and "needs" not in pages
    assert pages["permissions"] == {"contents": "read", "pages": "write", "id-token": "write"}
    assert pages["environment"] == {"name": "github-pages"}
    assert [s.get("uses") for s in pages["steps"]] == ["actions/checkout@v5", "actions/checkout@v5", "./.lsio-workflows/.github/actions/pages"]
    for job in ("prepare", "arch", "publish"):
        assert NOT_PAGES in wf["jobs"][job]["if"], job
    action = load(ROOT / ".github" / "actions" / "pages" / "action.yml")
    deploys = [s for s in action["runs"]["steps"] if str(s.get("uses", "")).startswith("actions/deploy-pages@")]
    uploads = [s for s in action["runs"]["steps"] if str(s.get("uses", "")).startswith("actions/upload-pages-artifact@")]
    assert len(deploys) == len(uploads) == 3
    # Artifact names are unique within a run; each deploy takes the upload before it.
    assert [u.get("with", {}).get("name", "github-pages") for u in uploads] == ["github-pages", "github-pages-2", "github-pages-3"]
    assert [d.get("with", {}).get("artifact_name", "github-pages") for d in deploys] == ["github-pages", "github-pages-2", "github-pages-3"]
    assert all(s.get("continue-on-error") for s in action["runs"]["steps"] if s.get("id") != "result")
    tpl = load(ROOT / "templates" / "build.yml")
    assert tpl["jobs"]["build"]["permissions"]["pages"] == "write"
    assert "pages" in tpl[True]["workflow_dispatch"]["inputs"]["reason"]["options"]


@pytest.mark.parametrize("workflow", ["image-build.yml", "mod-build.yml"])
def test_cheap_network_steps_get_a_second_attempt(workflow: str) -> None:
    """Checkouts are pairs (the first continues on error, its twin runs when it failed) and the artifact steps go
    through the retrying composites, so a flaky clone or artifact transfer costs seconds, not a self-healing re-run."""
    wf = load(ROOT / ".github" / "workflows" / workflow)
    for job_name, job in wf["jobs"].items():
        steps = job["steps"]
        for index, step in enumerate(steps):
            uses = str(step.get("uses", ""))
            assert not uses.startswith(("actions/download-artifact", "actions/upload-artifact")), (job_name, step["name"])
            if not uses.startswith("actions/checkout@") or step["name"].endswith(" (retry)"):
                continue
            assert step["continue-on-error"] is True and step.get("id"), (job_name, step["name"])
            twin = steps[index + 1]
            assert twin["name"] == f"{step['name']} (retry)" and twin["uses"] == uses and twin["with"] == step["with"]
            assert f"steps.{step['id']}.outcome == 'failure'" in twin["if"], (job_name, step["name"])
            if "if" in step:
                assert step["if"] in twin["if"], (job_name, step["name"])
    for name in ("artifact-download", "artifact-upload"):
        action = load(ROOT / ".github" / "actions" / name / "action.yml")
        attempts = [s for s in action["runs"]["steps"] if str(s.get("uses", "")).startswith("actions/")]
        assert len(attempts) == 2 and attempts[0]["continue-on-error"] is True
        assert attempts[1]["if"] == "steps.first.outcome == 'failure'"
    publish = _steps(wf, "publish")
    if workflow == "image-build.yml":
        # A failed checkout only matters to the package comparison, which fails on its own; the outcome reads that.
        assert publish["outcome"]["env"]["COMPARE"] == "${{ steps.compare.outcome }}"
