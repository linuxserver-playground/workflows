#!/usr/bin/env python3
"""
The trusted half of a pull request build: the report comment, written from the base repository.

    pr_report.py resolve                  which pull request and Build run this event is about (step outputs)
    pr_report.py comment --bundle DIR     compose the report, upload the screenshots, post or update the comment

This runs from the generated pr-report.yml on workflow_run and pull_request_target, with the base repository's
secrets and a write token. It never checks out or runs pull request code. The pull request and its run are found
through the API, never taken from the artifacts. What the Build run left (its ``bundle`` artifact) is read as
untrusted data: every file is size-capped, archives are only read member by member, screenshots must be PNGs of a
sane size, and every string that reaches the comment is escaped (code blocks get a fence longer than any backtick
run in their content). Screenshots are uploaded as comment attachments with ATTACHMENTS_TOKEN, a user token (the
workflow token cannot); without it the comment points at the artifact instead.

``compose`` and the readers are pure and unit-tested; GitHub calls go through ``gh api``.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import subprocess
import sys
import tarfile
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MARKER = "<!-- lsio-build-report -->"
BOT = "github-actions[bot]"
BUILD_WORKFLOW = ".github/workflows/build.yml"
ARCHES = ("amd64", "arm64")
ARCH_PREFIX = {"amd64": "amd64", "arm64": "arm64v8"}
APPROVAL_LABEL = os.environ.get("APPROVAL_LABEL", "safe-to-publish")

MAX_JSON = 4 << 20
MAX_PNG = 8 << 20
MAX_LOG = 4 << 20
MAX_TABLE = 4 << 20
MAX_PNG_SIDE = 10000
LOG_TAIL = 60
MAX_BODY = 60000
"""GitHub's comment limit is 65536 characters; this leaves room for the trimming notes."""
ATTACHMENT_PREFIX = "https://github.com/user-attachments/assets/"
WARNING = (
    "> [!WARNING]\n"
    "> This image is an **unreviewed pull request build**, published under the `untrusted-pr` namespace. "
    "It is not for production use, never receives a `latest` tag and is deleted when the pull request closes."
)

Json = dict[str, Any]


# ------------------------------------------------------------------------------------------------ escaping


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]|\x1b\[[0-9;?]*[A-Za-z]")
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+!|~>-])")


def text(value: Any, limit: int = 200) -> str:
    """Untrusted text for a Markdown table cell or sentence: one line, HTML and Markdown escaped, no mentions."""
    s = _CONTROL.sub("", str(value if value is not None else ""))
    s = " ".join(s.split())
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    s = html.escape(s, quote=False)
    s = _MD_SPECIAL.sub(r"\\\1", s)
    return s.replace("@", "@\u200b")


def code_block(content: str, lang: str = "") -> str:
    """A fenced block whose fence is longer than any backtick run inside, so the content cannot close it."""
    content = _CONTROL.sub("", content.replace("\r\n", "\n").replace("\t", "    ")).rstrip("\n")
    longest = max((len(m) for m in re.findall(r"`+", content)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{lang}\n{content}\n{fence}"


def code(value: Any, limit: int = 80) -> str:
    """Inline code for a tag, digest, version or branch: only characters such names use survive."""
    s = re.sub(r"[^\w.+:~@/-]", "", str(value if value is not None else ""))[:limit]
    return f"`{s}`"


# ------------------------------------------------------------------------------------------------ readers


def read_json(path: Path, cap: int = MAX_JSON) -> Any:
    try:
        if not path.is_file() or path.stat().st_size > cap:
            return None
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def read_text(path: Path, cap: int = MAX_TABLE) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > cap:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def tar_member(path: Path, name: str, cap: int) -> bytes | None:
    """One regular file from a .tar.gz by exact name, read in memory; nothing is extracted to disk."""
    try:
        with tarfile.open(path, "r:gz") as tar:
            for member in tar:
                if member.name.removeprefix("./") != name:
                    continue
                if not member.isreg() or member.size > cap:
                    return None
                fh = tar.extractfile(member)
                return fh.read(cap + 1)[:cap] if fh else None
    except (OSError, tarfile.TarError, EOFError):
        return None
    return None


def png_size(data: bytes | None) -> tuple[int, int] | None:
    """(width, height) of a well-formed PNG within the size limits, else None."""
    if not data or len(data) > MAX_PNG or not data.startswith(b"\x89PNG\r\n\x1a\n") or data[12:16] != b"IHDR":
        return None
    width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if not (0 < width <= MAX_PNG_SIDE and 0 < height <= MAX_PNG_SIDE):
        return None
    return width, height


def parse_table(content: str) -> dict[str, str]:
    """syft's NAME VERSION [TYPE] table -> {name: version}; the first version of a duplicate name wins."""
    packages: dict[str, str] = {}
    lines = content.strip().splitlines()
    if lines and lines[0].split()[:2] == ["NAME", "VERSION"]:
        lines = lines[1:]
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[0] not in packages:
            packages[parts[0]] = parts[1]
    return packages


def diff_lines(old: dict[str, str], new: dict[str, str]) -> list[str]:
    out: list[str] = []
    for name in sorted(set(old) | set(new)):
        before, after = old.get(name), new.get(name)
        if before is None:
            out.append(f"+ {name} {after}")
        elif after is None:
            out.append(f"- {name} {before}")
        elif before != after:
            out.append(f"~ {name} {before} -> {after}")
    return out


# ------------------------------------------------------------------------------------------------ the report


@dataclass
class ArchData:
    arch: str
    tests: list[Json] = field(default_factory=list)
    runtime: Json = field(default_factory=dict)
    success: bool | None = None
    startup: float | None = None
    screenshot: bytes | None = None
    screenshot_url: str | None = None
    log_tail: str = ""
    packages: dict[str, str] | None = None


@dataclass
class Context:
    """Everything the comment needs. Values from `resolve` are trusted; `arch`, `bundle` came from the build."""

    image: str
    primary_tag: str
    image_path: str
    head_sha: str
    run_url: str
    conclusion: str
    fork: bool
    approved: bool
    published: bool
    image_exists: bool
    artifact_name: str = "bundle"
    bundle: Json = field(default_factory=dict)
    arches: dict[str, ArchData] = field(default_factory=dict)
    base_ref: str = ""
    base_release: str = ""
    base_packages: dict[str, dict[str, str]] = field(default_factory=dict)
    attachments: bool = False


def load_arch(bundle_dir: Path, arch: str) -> ArchData | None:
    report = read_json(bundle_dir / f"test.{arch}.json")
    tarball = bundle_dir / f"test.{arch}.tar.gz"
    if not isinstance(report, dict) and not tarball.is_file():
        return None
    data = ArchData(arch)
    if isinstance(report, dict):
        data.tests = [t for t in report.get("tests") or [] if isinstance(t, dict)][:40]
        data.runtime = report.get("runtime") if isinstance(report.get("runtime"), dict) else {}
        data.success = report.get("success") if isinstance(report.get("success"), bool) else None
        startup = report.get("startup_seconds")
        data.startup = float(startup) if isinstance(startup, (int, float)) else None
    if tarball.is_file():
        shot = tar_member(tarball, "screenshot.png", MAX_PNG)
        data.screenshot = shot if png_size(shot) else None
        log = tar_member(tarball, "container.log", MAX_LOG)
        if log:
            lines = log.decode("utf-8", "replace").splitlines()[-LOG_TAIL:]
            data.log_tail = "\n".join(line[:300] for line in lines)
    table = read_text(bundle_dir / f"package_versions.{arch}.txt")
    data.packages = parse_table(table) if table else None
    return data


def _status(value: Any) -> str:
    s = str(value or "").upper()
    return {"PASS": "pass", "FAIL": "**FAIL**", "WARN": "warn", "SKIP": "skip"}.get(s, text(value, 12))


def _size(value: Any) -> str:
    return f"{value / 1024 / 1024:.1f} MiB" if isinstance(value, int) and value > 0 else "n/a"


def _digest(value: Any) -> str:
    return code(str(value)[:19]) if isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value) else "n/a"


def compose(ctx: Context) -> str:
    arches = [a for a in ARCHES if a in ctx.arches]
    sha7 = ctx.head_sha[:7]
    version = (ctx.bundle.get("version") or {}) if isinstance(ctx.bundle.get("version"), dict) else {}
    upstream = version.get("upstream")
    tests_ok = [a for a in arches if ctx.arches[a].success is True]
    tests_bad = [a for a in arches if ctx.arches[a].success is False]
    if ctx.conclusion == "success":
        verdict = "**Build and tests passed**"
    elif tests_bad:
        verdict = f"**Tests failed** on {', '.join(tests_bad)}"
    elif ctx.conclusion == "cancelled":
        verdict = "**Build cancelled**"
    else:
        verdict = "**Build failed**"
    lines = [
        MARKER,
        f"### Build report for {code(ctx.image)} · {code(ctx.primary_tag)}",
        "",
        " · ".join(
            x
            for x in (
                verdict,
                f"commit {code(sha7)}",
                f"upstream {code(upstream, 60)}" if upstream else "",
                f"[workflow run]({ctx.run_url})",
            )
            if x
        ),
        "",
    ]

    # Tests: one row per test, one column per architecture.
    names: list[str] = []
    for a in arches:
        for t in ctx.arches[a].tests:
            if t.get("name") and t["name"] not in names:
                names.append(str(t["name"]))
    if names:
        lines += ["| Test | " + " | ".join(arches) + " |", "| --- |" + " --- |" * len(arches)]
        for name in names:
            cells = []
            for a in arches:
                t = next((t for t in ctx.arches[a].tests if t.get("name") == name), None)
                if t is None:
                    cells.append("")
                    continue
                message = text(t.get("message"), 90)
                cells.append(f"{_status(t.get('status'))}{' · ' + message if message else ''}")
            lines.append(f"| {text(name, 40)} | " + " | ".join(cells) + " |")
        lines.append("")
    elif tests_ok or tests_bad or ctx.conclusion != "success":
        lines += ["The build produced no test reports; see the workflow run.", ""]

    # Screenshots.
    shots = [a for a in arches if ctx.arches[a].screenshot]
    if shots:
        urls = [a for a in shots if ctx.arches[a].screenshot_url]
        if urls:
            lines += ["| " + " | ".join(urls) + " |", "|" + " --- |" * len(urls)]
            lines.append(
                "| "
                + " | ".join(f'<img src="{ctx.arches[a].screenshot_url}" width="420" alt="{a} screenshot">' for a in urls)
                + " |"
            )
            lines.append("")
        missing = [a for a in shots if not ctx.arches[a].screenshot_url]
        if missing:
            why = "uploads need the ATTACHMENTS_TOKEN secret" if not ctx.attachments else "the upload failed"
            lines += [f"Screenshot for {', '.join(missing)} is in `test.<arch>.tar.gz` of the `{ctx.artifact_name}` artifact ({why}).", ""]

    # Platforms and what the container did at idle.
    platforms = {p.get("arch"): p for p in ctx.bundle.get("platforms") or [] if isinstance(p, dict)}
    if arches or platforms:
        lines += ["| Arch | Digest | Compressed | Layers | Startup | Memory | Processes | TCP ports |", "| --- |" + " --- |" * 7]
        for a in [x for x in ARCHES if x in arches or x in platforms]:
            p = platforms.get(a, {})
            data = ctx.arches.get(a) or ArchData(a)
            rt = data.runtime
            rss = rt.get("rss_bytes")
            ports = rt.get("listening_tcp") if isinstance(rt.get("listening_tcp"), list) else []
            layers = p.get("layers") if isinstance(p.get("layers"), int) else "n/a"
            startup = f"{data.startup:.0f} s" if data.startup is not None else "n/a"
            pids = rt.get("pids") if isinstance(rt.get("pids"), int) else "n/a"
            lines.append(
                f"| {a} | {_digest(p.get('digest'))} | {_size(p.get('size'))} | {layers} | {startup} | "
                f"{_size(rss) if isinstance(rss, int) else 'n/a'} | {pids} | "
                f"{text(', '.join(str(x) for x in ports[:12] if isinstance(x, int)), 80) or 'none'} |"
            )
        lines.append("")

    # Package changes against the target branch's latest release.
    if ctx.base_release:
        for a in arches:
            new = ctx.arches[a].packages
            old = ctx.base_packages.get(a)
            if new is None or old is None:
                continue
            changes = diff_lines(old, new)
            summary = f"Package changes on {a} against {code(ctx.base_release)}: {len(changes) or 'none'}"
            if changes:
                shown = changes[:300]
                more = f"\n… and {len(changes) - 300} more" if len(changes) > 300 else ""
                lines += [f"<details><summary>{summary}</summary>", "", code_block("\n".join(shown) + more, "diff"), "", "</details>", ""]
            else:
                lines += [summary, ""]
    elif ctx.base_ref:
        lines += [f"No release of {code(ctx.base_ref, 60)} to compare packages against yet.", ""]

    # Container logs.
    for a in arches:
        tail = ctx.arches[a].log_tail
        if tail:
            lines += [f"<details><summary>Container log on {a} (last {LOG_TAIL} lines)</summary>", "", code_block(tail), "", "</details>", ""]

    # The image.
    if ctx.image_exists:
        lines += ["Pull the quarantined image:", "", code_block(f"docker pull {ctx.image_path}:{ctx.primary_tag}"), "", WARNING]
    elif ctx.fork and ctx.conclusion == "success" and not ctx.approved:
        lines.append(
            f"This pull request comes from a fork, so its image is published only after a maintainer adds the "
            f"`{APPROVAL_LABEL}` label (a new push removes it). Until then it is the `image-<arch>` artifact of the run."
        )
    elif ctx.fork and ctx.approved and not ctx.published:
        lines.append(f"`{APPROVAL_LABEL}` is set, but publishing the image failed; see the PR report run.")
    else:
        lines.append("No image was published for this build.")
    lines += ["", f"Everything the build wrote (`build.json`, test reports, SBOMs, file trees) is the `{ctx.artifact_name}` artifact of the run."]
    return _fit("\n".join(lines) + "\n")


def _fit(body: str) -> str:
    """Drop the collapsible sections, last first, until the body fits a GitHub comment."""
    while len(body) > MAX_BODY and "<details>" in body:
        start = body.rfind("<details>")
        end = body.find("</details>", start)
        body = body[:start] + "_A section was left out to fit GitHub's comment limit._" + body[end + len("</details>") :]
    return body if len(body) <= MAX_BODY else body[: MAX_BODY - 40] + "\n\n_Truncated to fit._\n"


# ------------------------------------------------------------------------------------------------ GitHub


def gh_api(args: list[str], body: Json | None = None) -> Any:
    cmd = ["gh", "api", *args]
    proc = subprocess.run(cmd, input=json.dumps(body) if body is not None else None, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"gh api {' '.join(args)}: {proc.stderr.strip()[:500]}")
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def outputs(values: dict[str, Any]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    lines = [f"{k}={'true' if v is True else 'false' if v is False else v}" for k, v in values.items()]
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))


def image_name(repo: str, ref: str) -> str:
    """`name:` from image.yml on the base branch (trusted), else the repository name without docker-."""
    try:
        data = gh_api([f"repos/{repo}/contents/image.yml?ref={urllib.parse.quote(ref, safe='')}"])
        content = base64.b64decode(data.get("content", "")).decode("utf-8", "replace")
        m = re.search(r'^name:\s*"?([A-Za-z0-9._-]+)"?\s*(#.*)?$', content, re.MULTILINE)
        if m:
            return m.group(1)
    except (RuntimeError, ValueError, AttributeError):
        pass
    return repo.split("/", 1)[1].removeprefix("docker-")


def find_pull_request(repo: str, run: Json) -> Json | None:
    """The open pull request whose head is this run's commit. Fork runs carry no pull_requests list."""
    head_sha = run.get("head_sha")
    for pr in run.get("pull_requests") or []:
        if pr.get("head", {}).get("sha") == head_sha and pr.get("number"):
            return gh_api([f"repos/{repo}/pulls/{int(pr['number'])}"])
    owner = ((run.get("head_repository") or {}).get("owner") or {}).get("login")
    branch = run.get("head_branch")
    if not owner or not branch:
        return None
    head = urllib.parse.quote(f"{owner}:{branch}", safe="")
    for pr in gh_api([f"repos/{repo}/pulls?state=open&head={head}&per_page=100"]) or []:
        if pr.get("head", {}).get("sha") == head_sha:
            return pr
    return None


def latest_build(repo: str, head_sha: str) -> Json | None:
    data = gh_api([f"repos/{repo}/actions/runs?event=pull_request&head_sha={head_sha}&per_page=30"]) or {}
    runs = [r for r in data.get("workflow_runs") or [] if str(r.get("path", "")).split("@")[0] == BUILD_WORKFLOW]
    runs.sort(key=lambda r: (r.get("created_at") or "", r.get("id") or 0), reverse=True)
    return runs[0] if runs else None


def resolve(event_name: str, payload: Json, repo: str) -> Json:
    """What this event asks for. `action` is `report` or `skip` (with a `reason`)."""
    if event_name == "workflow_run":
        run = payload.get("workflow_run") or {}
        if run.get("event") != "pull_request" or str(run.get("path", "")).split("@")[0] != BUILD_WORKFLOW:
            return {"action": "skip", "reason": "not a pull request build"}
        pr = find_pull_request(repo, run)
    elif event_name == "pull_request_target":
        pr = gh_api([f"repos/{repo}/pulls/{int(payload['pull_request']['number'])}"])
        run = latest_build(repo, pr["head"]["sha"]) or {}
        if run.get("status") != "completed":
            return {"action": "skip", "reason": "the build of the head commit has not finished; it reports when it does"}
    else:
        return {"action": "skip", "reason": f"unexpected event {event_name}"}
    if not pr or pr.get("state") != "open":
        return {"action": "skip", "reason": "no open pull request for this run"}
    head_sha = pr["head"]["sha"]
    if run.get("head_sha") != head_sha:
        return {"action": "skip", "reason": "the pull request moved on; the newer build reports"}
    fork = (pr["head"].get("repo") or {}).get("full_name") != repo
    approved = APPROVAL_LABEL in [label.get("name") for label in pr.get("labels") or []]
    name = image_name(repo, pr["base"]["ref"])
    org = repo.split("/", 1)[0]
    success = run.get("conclusion") == "success"
    return {
        "action": "report",
        "pr": int(pr["number"]),
        "head_sha": head_sha,
        "base_ref": pr["base"]["ref"],
        "fork": fork,
        "approved": approved,
        "publish": fork and approved and success,
        "run_id": int(run["id"]),
        "run_url": run.get("html_url") or f"https://github.com/{repo}/actions/runs/{int(run['id'])}",
        "conclusion": run.get("conclusion") or "unknown",
        "name": name,
        "image_path": f"ghcr.io/{org}/untrusted-pr/{name}",
        "primary_tag": f"pr-{int(pr['number'])}-{head_sha[:7]}",
    }


def base_release(repo: str, base_ref: str) -> Json | None:
    """The newest published release made from the target branch (target_commitish is the branch name)."""
    for release in gh_api([f"repos/{repo}/releases?per_page=50"]) or []:
        if not release.get("draft") and release.get("target_commitish") == base_ref:
            return release
    return None


def fetch(url: str, cap: int = MAX_TABLE) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            return response.read(cap + 1)[:cap].decode("utf-8", "replace")
    except OSError:
        return None


def upload_attachment(token: str, repository_id: int, name: str, data: bytes) -> str | None:
    """Upload a PNG as a comment attachment (the web UI's endpoint; it takes a user token, not the workflow's)."""
    query = urllib.parse.urlencode({"name": name, "content_type": "image/png", "repository_id": repository_id})
    request = urllib.request.Request(
        f"https://uploads.github.com/user-attachments/assets?{query}",
        data=data,
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "image/png"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            url = str(json.load(response).get("url", ""))
    except (OSError, ValueError) as err:
        print(f"::warning::screenshot upload failed: {err}")
        return None
    return url if url.startswith(ATTACHMENT_PREFIX) and re.fullmatch(r"[\w:/.-]+", url) else None


def image_exists(ref: str) -> bool:
    proc = subprocess.run(["docker", "buildx", "imagetools", "inspect", ref], capture_output=True, text=True, check=False)
    return proc.returncode == 0


def upsert(repo: str, pr: int, body: str) -> str:
    comments = gh_api(["--paginate", f"repos/{repo}/issues/{pr}/comments?per_page=100"]) or []
    ours = [c for c in comments if MARKER in (c.get("body") or "") and (c.get("user") or {}).get("login") == BOT]
    if ours:
        gh_api(["--method", "PATCH", f"repos/{repo}/issues/comments/{ours[0]['id']}", "--input", "-"], {"body": body})
        return "updated"
    gh_api(["--method", "POST", f"repos/{repo}/issues/{pr}/comments", "--input", "-"], {"body": body})
    return "created"


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() == "true"


def cmd_comment(bundle_dir: Path) -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    pr = int(os.environ["PR"])
    ctx = Context(
        image=os.environ["IMAGE_NAME"],
        primary_tag=os.environ["PRIMARY_TAG"],
        image_path=os.environ["IMAGE_PATH"],
        head_sha=os.environ["HEAD_SHA"],
        run_url=os.environ["RUN_URL"],
        conclusion=os.environ.get("CONCLUSION", "unknown"),
        fork=env_flag("FORK"),
        approved=env_flag("APPROVED"),
        published=env_flag("PUBLISHED"),
        image_exists=False,
        base_ref=os.environ.get("BASE_REF", ""),
    )
    bundle = read_json(bundle_dir / "build.json")
    ctx.bundle = bundle if isinstance(bundle, dict) else {}
    for arch in ARCHES:
        data = load_arch(bundle_dir, arch)
        if data:
            ctx.arches[arch] = data
    ctx.image_exists = (not ctx.fork or ctx.published) and image_exists(f"{ctx.image_path}:{ctx.primary_tag}")

    release = base_release(repo, ctx.base_ref) if ctx.base_ref else None
    if release:
        ctx.base_release = str(release.get("tag_name", ""))
        assets = {a.get("name"): a.get("browser_download_url") for a in release.get("assets") or []}
        for arch in ctx.arches:
            url = assets.get(f"package_versions.{arch}.txt")
            content = fetch(url) if url else None
            if content is not None:
                ctx.base_packages[arch] = parse_table(content)

    token = os.environ.get("ATTACHMENTS_TOKEN", "").strip()
    ctx.attachments = bool(token)
    if token:
        repository_id = int(gh_api([f"repos/{repo}"])["id"])
        for arch, data in ctx.arches.items():
            if data.screenshot:
                data.screenshot_url = upload_attachment(token, repository_id, f"{ctx.primary_tag}-{arch}.png", data.screenshot)

    body = compose(ctx)
    try:
        print(f"comment {upsert(repo, pr, body)} ({len(body)} characters)")
    except RuntimeError as err:
        print(f"::error::could not post the report: {err}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("resolve")
    comment = sub.add_parser("comment")
    comment.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "resolve":
        payload = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        result = resolve(os.environ["GITHUB_EVENT_NAME"], payload, os.environ["GITHUB_REPOSITORY"])
        if result["action"] == "skip":
            print(f"::notice::nothing to report: {result['reason']}")
        outputs(result)
        return 0
    return cmd_comment(args.bundle)


if __name__ == "__main__":
    sys.exit(main())
