#!/usr/bin/env python3
"""
Delete old versions of the ``build/*`` staging packages (the intermediate per-arch images the arch
jobs push before publish-manifest creates the released tags from them).

    cleanup_staging.py --org linuxserver-playground [--max-age-days 7] [--dry-run]

Lists every container package of the organisation whose name starts with ``build/`` and deletes the
versions older than ``--max-age-days`` through the packages API (``gh api`` with GH_TOKEN). Deleting
organisation package versions needs ``delete:packages``, which GITHUB_TOKEN does not have, so the caller
passes PACKAGES_TOKEN. ``select_old_versions`` is pure and unit-tested.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import urllib.parse
from typing import Any

STAGING_PREFIX = "build/"


def _parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def select_old_versions(versions: list[dict[str, Any]], now: dt.datetime, max_age_days: int) -> list[dict[str, Any]]:
    """Versions whose last update is older than the cut-off. Versions without a timestamp are kept."""
    cutoff = now - dt.timedelta(days=max_age_days)
    chosen = []
    for v in versions:
        stamp = v.get("updated_at") or v.get("created_at")
        if not stamp:
            continue
        if _parse_time(stamp) < cutoff:
            chosen.append(v)
    return chosen


def staging_packages(packages: list[dict[str, Any]]) -> list[str]:
    return sorted(p["name"] for p in packages if str(p.get("name", "")).startswith(STAGING_PREFIX))


def gh_api(args: list[str]) -> Any:
    proc = subprocess.run(["gh", "api", *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def _flatten(pages: Any) -> list[dict[str, Any]]:
    return [v for page in (pages or []) for v in page]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--org", required=True)
    ap.add_argument("--max-age-days", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    now = dt.datetime.now(tz=dt.UTC)

    try:
        pages = gh_api(["--paginate", "--slurp", f"orgs/{args.org}/packages?package_type=container&per_page=100"])
    except RuntimeError as exc:
        print(f"::warning::could not list the organisation's packages: {exc}")
        return 0
    names = staging_packages(_flatten(pages))
    if not names:
        print(f"no {STAGING_PREFIX}* packages in {args.org}")
        return 0

    deleted = failures = 0
    for name in names:
        base = f"orgs/{args.org}/packages/container/{urllib.parse.quote(name, safe='')}"
        try:
            versions = _flatten(gh_api(["--paginate", "--slurp", f"{base}/versions?per_page=100"]))
        except RuntimeError as exc:
            print(f"::warning::could not list versions of {name}: {exc}")
            continue
        old = select_old_versions(versions, now, args.max_age_days)
        print(f"{name}: {len(versions)} version(s), {len(old)} older than {args.max_age_days} day(s)")
        for v in old:
            tags = ",".join(((v.get("metadata") or {}).get("container") or {}).get("tags") or [])
            if args.dry_run:
                print(f"would delete {name} version {v['id']} ({tags or 'untagged'}, {v.get('updated_at')})")
                continue
            try:
                gh_api(["--method", "DELETE", f"{base}/versions/{v['id']}"])
                deleted += 1
                print(f"deleted {name} version {v['id']} ({tags or 'untagged'})")
            except RuntimeError as exc:
                failures += 1
                print(f"::warning::could not delete {name} version {v['id']}: {exc}")
    print(f"deleted {deleted} version(s), {failures} failure(s)")
    if failures:
        print("::warning::some versions were not deleted; the token needs delete:packages on the organisation")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
