#!/usr/bin/env python3
"""
POST a control-plane event (control-event.schema.json) signed with HMAC-SHA256.

Configuration is by environment:
  CONTROL_URL, CONTROL_EVENTS_SECRET      empty CONTROL_URL -> do nothing, exit 0
  EVENT_TYPE (build.summary|build.started), REPOSITORY, RUN_ID, RUN_URL
  IMAGE, BRANCH, MODE, PR_NUMBER, REASON
  RESULT (success|failure|cancelled|skipped), FAILED_JOB, BUNDLE_PATH, RELEASE_URL, ARTIFACT_URL

A failure to deliver is a warning, never a build failure: the control plane reconciles from
GitHub on its own schedule. Network failures and 5xx answers are retried (scripts/netretry.py), three
attempts over about half a minute so a control plane that is down does not hold every build's runner;
control de-duplicates a repeated POST by its body, which is built once.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import http.client
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

import gha  # noqa: E402
import netretry  # noqa: E402


ATTEMPTS = 3


def build_event(env: dict[str, str], bundle: dict[str, Any] | None, sent: str | None = None) -> dict[str, Any]:
    event: dict[str, Any] = {
        "schema": 1,
        "type": env.get("EVENT_TYPE") or "build.summary",
        "sent": sent or dt.datetime.now(tz=dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repository": env["REPOSITORY"],
        "run_id": int(env["RUN_ID"]),
    }
    optional = {"run_url": "RUN_URL", "image": "IMAGE", "branch": "BRANCH", "mode": "MODE", "reason": "REASON"}
    for key, var in optional.items():
        if env.get(var):
            event[key] = env[var]
    if env.get("PR_NUMBER"):
        event["pull_request"] = int(env["PR_NUMBER"])
    if event["type"] == "build.summary":
        result = env.get("RESULT") or "failure"
        event["result"] = result if result in {"success", "failure", "cancelled", "skipped"} else "failure"
        event["bundle"] = bundle
        if env.get("FAILED_JOB"):
            event["failed_job"] = env["FAILED_JOB"]
        if env.get("RELEASE_URL"):
            event["release_url"] = env["RELEASE_URL"]
        if env.get("ARTIFACT_URL"):
            event["artifact_url"] = env["ARTIFACT_URL"]
    return event


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def post(url: str, body: bytes, signature: str, timeout: int = 30) -> int:
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "X-Control-Signature": signature, "User-Agent": "linuxserver-playground-workflows/1"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return int(resp.status)


def main() -> int:
    env = dict(os.environ)
    control_url = (env.get("CONTROL_URL") or "").strip()
    if not control_url:
        print("CONTROL_URL is empty; not notifying the control plane")
        return 0
    secret = env.get("CONTROL_EVENTS_SECRET") or ""
    if not secret:
        gha.warning("CONTROL_URL is set but CONTROL_EVENTS_SECRET is empty; not notifying")
        return 0
    bundle = None
    bundle_path = env.get("BUNDLE_PATH") or ""
    if bundle_path and Path(bundle_path).is_file():
        try:
            bundle = json.loads(Path(bundle_path).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            gha.warning(f"build.json unreadable ({exc}); sending bundle: null")
    event = build_event(env, bundle)
    body = json.dumps(event, separators=(",", ":"), sort_keys=True).encode("utf-8")
    url = control_url.rstrip("/") + "/events"
    try:
        signature = sign(body, secret)
        status = netretry.retry(lambda: post(url, body, signature), what=f"POST {url}", attempts=ATTEMPTS)
        print(f"POST {url} -> {status}")
    except urllib.error.HTTPError as exc:
        gha.warning(f"control plane returned HTTP {exc.code} for {url}")
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        gha.warning(f"could not reach the control plane at {url}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
