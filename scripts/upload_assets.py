#!/usr/bin/env python3
"""
Attach a build's bundle to its GitHub Release so that the Release ends up holding exactly these files, or say which
it does not.

    upload_assets.py --repo <owner/repo> --tag <tag> FILE...

``gh release upload --clobber`` under ``retry`` was not enough when GitHub's release asset API had a bad day
(2026-11-04): ``--clobber`` deletes each existing asset and then uploads its replacement, and a retry of the whole
command after a 500 found names it had not expected (422 "already exists"), while a 404 on a release created seconds
before was not retried at all. docker-heimdall's v2.8.4-ls7 was left without build.json and five other files, and
docker-python's ha-alpine324-3.14.10-ls3 without four.

So this works in rounds and trusts only what the Release says. Each round lists the Release's assets and takes as done
every file whose asset is ``uploaded`` with the local file's sha256 (GitHub's ``digest``). For each file still to do it
deletes a stale or half-uploaded asset of that name, then uploads the file. A failed call leaves the file for the next
round; nothing is assumed from a call's answer. An asset that already matches is never deleted, so a re-run of the
same bundle touches nothing, and a delete that keeps failing leaves the old asset in place rather than none. Between
rounds it waits as netretry does (about 10, 20, 40, 80 s, then 120 s, with jitter). When the rounds run out it fails
and names each file that is missing or different.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

import netretry

ROUNDS = 8
"""Rounds of list, delete and upload: with netretry's waits about eight and a half minutes before giving up."""


class Releases(Protocol):
    def assets(self) -> list[dict[str, Any]]: ...
    def delete(self, asset_id: int) -> None: ...
    def upload(self, path: Path) -> None: ...


class CallFailed(RuntimeError):
    pass


class GhReleases:
    """The Release behind ``gh``. Every call is made once; the rounds are the retry."""

    def __init__(self, repo: str, tag: str) -> None:
        self.repo = repo
        self.tag = tag

    def _gh(self, *args: str) -> str:
        proc = netretry.run(["gh", *args], attempts=1)
        if proc.returncode != 0:
            raise CallFailed(f"gh {' '.join(args[:3])}: {(proc.stderr or '').strip() or f'exit {proc.returncode}'}")
        return proc.stdout

    def assets(self) -> list[dict[str, Any]]:
        # The release's own endpoint lists every asset (the release asset limit is far above a bundle).
        release = json.loads(self._gh("api", f"repos/{self.repo}/releases/tags/{self.tag}"))
        return list(release.get("assets") or [])

    def delete(self, asset_id: int) -> None:
        try:
            self._gh("api", "--method", "DELETE", f"repos/{self.repo}/releases/assets/{asset_id}")
        except CallFailed as exc:
            if "HTTP 404" not in str(exc):  # already gone: what the delete wanted
                raise

    def upload(self, path: Path) -> None:
        self._gh("release", "upload", self.tag, str(path), "--repo", self.repo)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def outstanding(files: dict[str, tuple[Path, str]], assets: list[dict[str, Any]]) -> dict[str, dict[str, Any] | None]:
    """The files the Release does not hold yet, each with its current asset of that name (or None)."""
    by_name = {a.get("name"): a for a in assets}
    todo: dict[str, dict[str, Any] | None] = {}
    for name, (_, digest) in files.items():
        asset = by_name.get(name)
        if asset and asset.get("state") == "uploaded" and asset.get("digest") == digest:
            continue
        todo[name] = asset
    return todo


def attach(
    releases: Releases,
    paths: Sequence[Path],
    *,
    rounds: int = ROUNDS,
    sleep: Callable[[float], Any] = time.sleep,
) -> list[str]:
    """Bring the Release to these files. Returns the names it still lacks (empty when done)."""
    files = {p.name: (p, sha256(p)) for p in paths}
    waits = netretry.delays(rounds)
    todo: dict[str, dict[str, Any] | None] = dict.fromkeys(files)
    for n, wait in enumerate([*waits, None], start=1):
        try:
            todo = outstanding(files, releases.assets())
        except CallFailed as exc:
            # A Release created seconds ago can answer 404 for a moment; keep the last known state.
            _say(f"round {n}: could not list the release's assets ({exc})")
        else:
            if not todo:
                return []
            if wait is None:
                break
            for name, asset in todo.items():
                try:
                    if asset is not None:
                        releases.delete(int(asset["id"]))
                    releases.upload(files[name][0])
                except CallFailed as exc:
                    _say(f"round {n}: {name}: {exc}")
        if wait is None:
            break
        pause = netretry._jitter(wait)  # noqa: SLF001 - the same spread as every other retry
        _say(f"round {n}/{len(waits) + 1}: {len(todo)} asset(s) not confirmed; checking again in {pause:.0f}s...")
        sleep(pause)
    return sorted(todo)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--rounds", type=int, default=ROUNDS)
    ap.add_argument("files", nargs="+", type=Path)
    args = ap.parse_args(argv)
    missing = [str(p) for p in args.files if not p.is_file()]
    if missing:
        _say(f"no such file: {', '.join(missing)}")
        return 2
    left = attach(GhReleases(args.repo, args.tag), args.files, rounds=args.rounds)
    if left:
        _say(f"::error::release {args.tag} is missing or holds a different {', '.join(left)} after {args.rounds} rounds")
        return 1
    print(f"release {args.tag} holds all {len(args.files)} assets")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
