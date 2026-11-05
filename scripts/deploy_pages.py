#!/usr/bin/env python3
"""
Deploy an uploaded Pages artifact under a build version of its own: actions/deploy-pages with one change.

    python3 scripts/deploy_pages.py --artifact-id <id> [--attempt <n>]

actions/deploy-pages names every deployment after GITHUB_SHA (`pages_build_version`), which GitHub's API says must be
unique, and a step cannot change it: the runner writes the github context over a step's env. Pages answers a second
deployment of the same version with the first one's status, so a repository that deploys again without a new commit
gets "Reported success!" while the site keeps its older deploy. Since the hub's and the site's generated data moved to
their `data` branches (2026-10-27), main rarely moves between deploys: on 2026-11-05 the site served its 00:55 deploy
all day and the hub stayed on one deploy for over an hour, every run green or failing only its own serve check
(the hub's `scripts/pages_serves.sh`). An image repository's `reason=pages` run and its redeploys after `pages.py check`
reused the release's commit the same way.

So this sends the same requests as deploy-pages (POST /repos/{repo}/pages/deployments with the job's OIDC token, then
GET its status until it settles, cancelling on timeout) with `pages_build_version` set to
`<sha>-<run id>-<run attempt>-<attempt>`, unique per deploy. The job needs `pages: write` and `id-token: write` and the
github-pages environment, as for deploy-pages. Outputs `page_url` and `status`; exits 1 when the deploy did not
succeed. Network failures retry (netretry); a 4xx is final. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

import gha
import netretry

Json = dict[str, Any]
Request = Callable[[str, str, "Json | None", "dict[str, str]"], Json]

FINAL_ERRORS = {
    "deployment_failed": "Deployment failed, try again later.",
    "deployment_content_failed": "Artifact could not be deployed: hard links, symlinks or over 10 GB.",
    "deployment_cancelled": "Deployment cancelled.",
    "deployment_lost": "Deployment failed to report final status.",
}
TIMEOUT = 600.0  # deploy-pages' maximum
INTERVAL = 5.0
MAX_INTERVAL = 30.0


def http(method: str, url: str, body: Json | None, headers: dict[str, str]) -> Json:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": "lsio-workflows", **headers})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def build_version(env: dict[str, str], attempt: int) -> str:
    return f"{env['GITHUB_SHA']}-{env['GITHUB_RUN_ID']}-{env.get('GITHUB_RUN_ATTEMPT', '1')}-{attempt}"


def oidc_token(env: dict[str, str], request: Request) -> str:
    url, token = env.get("ACTIONS_ID_TOKEN_REQUEST_URL"), env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not url or not token:
        raise SystemExit("No OIDC token: the job needs `id-token: write`")
    got = netretry.retry(lambda: request("GET", url, None, {"Authorization": f"bearer {token}"}), what="the OIDC token")
    return str(got["value"])


def deploy(
    artifact_id: int,
    attempt: int,
    env: dict[str, str],
    request: Request = http,
    sleep: Callable[[float], Any] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[str, str]:
    """Create the deployment and wait for it to settle: (status, page_url)."""
    api = f"{env.get('GITHUB_API_URL', 'https://api.github.com')}/repos/{env['GITHUB_REPOSITORY']}/pages/deployments"
    auth = {"Authorization": f"Bearer {env['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"}
    version = build_version(env, attempt)
    payload = {"artifact_id": artifact_id, "pages_build_version": version, "oidc_token": oidc_token(env, request)}
    print(f"Creating Pages deployment of artifact {artifact_id} as build version {version}")
    created = netretry.retry(lambda: request("POST", api, payload, auth), what="creating the Pages deployment")
    deployment_id = str(created.get("id") or str(created.get("status_url", "")).rstrip("/").split("/")[-1] or version)
    page_url = str(created.get("page_url") or "")
    print(f"Created deployment {deployment_id}")

    start, interval, status = clock(), INTERVAL, "unknown_status"
    while clock() - start < TIMEOUT:
        sleep(interval * random.uniform(0.8, 1.2))
        got = netretry.retry(lambda: request("GET", f"{api}/{deployment_id}", None, auth), what="the deployment status")
        status = str(got.get("status") or "unknown_status")
        if status == "succeed":
            print("Reported success")
            return status, page_url
        if status in FINAL_ERRORS:
            gha.error(f"{FINAL_ERRORS[status]} ({status})")
            return status, ""
        print(f"Current status: {status}")
        interval = min(interval * 1.5, MAX_INTERVAL)

    gha.error(f"The deployment did not settle in {TIMEOUT:.0f} s (last status {status}); cancelling it")
    try:
        netretry.retry(lambda: request("POST", f"{api}/{deployment_id}/cancel", None, auth), what="cancelling it")
    except (urllib.error.URLError, OSError) as exc:
        gha.warning(f"Cancelling deployment {deployment_id} failed: {exc}")
    return "timed_out", ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1] if __doc__ else None)
    parser.add_argument("--artifact-id", type=int, required=True)
    parser.add_argument("--attempt", type=int, default=1, help="Which deploy of this run, so each has its own version")
    args = parser.parse_args(argv)
    try:
        status, page_url = deploy(args.artifact_id, args.attempt, dict(os.environ))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        gha.error(f"Pages refused the deployment: HTTP {exc.code} {detail}")
        status, page_url = f"http_{exc.code}", ""
    gha.set_output("status", status)
    gha.set_output("page_url", page_url)
    return 0 if status == "succeed" else 1


if __name__ == "__main__":
    sys.exit(main())
