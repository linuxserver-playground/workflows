#!/usr/bin/env python3
"""
Assemble build.json (bundle.schema.json) from the prepare outputs and the per-arch bundle directory.

    emit_bundle.py --out out/ --prepare prepare.json --repository owner/repo --run-id N --run-url URL \
        --workflows-ref v1 --ci-image <ci image> --reason push [--index-digest sha256:..] \
        [--control-url URL]

``prepare.json`` is ``toJSON(needs.prepare.outputs)``. The per-arch directory holds what build-arch
(``arch.<arch>.json``, ``dockerfile.<arch>``) and the ci harness (``test.<arch>.json``,
``packages.<arch>.json.gz``, ``image-config.<arch>.json``, ``init-graph.<arch>.json``,
``package_versions.<arch>.txt`` ...) wrote. Missing optional files are tolerated: a failed arch still
yields a record the hub can ingest.

The result is validated with ``python -m contracts bundle`` when the contracts package is importable.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

import gha  # noqa: E402

REASONS = {"push", "upstream", "packages", "rebuild", "manual", "pull_request"}
# Release asset names of CONVENTIONS §6, per arch.
ASSET_PATTERNS = ["packages.{a}.json.gz", "layers.{a}.json", "files.{a}.json.gz", "test.{a}.json", "test.{a}.tar.gz", "package_versions.{a}.txt"]
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _load_json(path: Path) -> Any:
    if not path.is_file():
        return None
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:  # type: ignore[operator]
        return json.load(fh)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _prune(obj: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in obj.items() if v not in (None, "", [], {})}


def _image_config(raw: Any) -> dict[str, Any]:
    if isinstance(raw, list):
        raw = raw[0] if raw else {}
    cfg = (raw or {}).get("Config") or {}
    return _prune(
        {
            "env": cfg.get("Env") or [],
            "exposed_ports": sorted((cfg.get("ExposedPorts") or {}).keys()),
            "volumes": sorted((cfg.get("Volumes") or {}).keys()),
            "entrypoint": cfg.get("Entrypoint") or [],
            "cmd": cfg.get("Cmd") or [],
            "user": cfg.get("User") or "",
            "working_dir": cfg.get("WorkingDir") or "",
        }
    )


def _init_graph(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    units_raw = raw.get("units") if isinstance(raw, dict) else raw
    units = []
    for u in units_raw or []:
        if not isinstance(u, dict) or "name" not in u:
            continue
        unit = {"name": u["name"], "type": u.get("type", "oneshot"), "depends_on": list(u.get("depends_on") or [])}
        if u.get("source") in {"base", "image", "mod"}:
            unit["source"] = u["source"]
        if u.get("script"):
            unit["script"] = u["script"]
        units.append(unit)
    graph: dict[str, Any] = {"units": units}
    if isinstance(raw, dict) and raw.get("d2"):
        graph["d2"] = raw["d2"]
    return graph if units or graph.get("d2") else None


def _packages_summary(out: Path, arch: str) -> dict[str, Any] | None:
    syft = _load_json(out / f"packages.{arch}.json.gz")
    pv = out / f"package_versions.{arch}.txt"
    if syft is None and not pv.is_file():
        return None
    artifacts = (syft or {}).get("artifacts") or []
    by_type: dict[str, int] = {}
    for a in artifacts:
        t = a.get("type") or "unknown"
        by_type[t] = by_type.get(t, 0) + 1
    distro = (syft or {}).get("distro") or {}
    distro_name = distro.get("prettyName") or " ".join(x for x in [distro.get("name"), distro.get("version")] if x) or "unknown"
    pv_hash = hashlib.md5(pv.read_bytes()).hexdigest()[:8] if pv.is_file() else "none"  # noqa: S324
    return _prune({"count": len(artifacts), "distro": distro_name, "hash": pv_hash, "by_type": by_type})


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_bundle(prepare: dict[str, Any], out: Path, *, repository: str, run_id: int, run_url: str, workflows_ref: str, ci_image: str, reason: str, index_digest: str = "", control_url: str = "", hub_url: str = "", site_url: str = "", created: str | None = None) -> dict[str, Any]:
    mode = prepare["mode"]
    platforms = json.loads(prepare["platforms"]) if isinstance(prepare.get("platforms"), str) else list(prepare.get("platforms") or [])
    arch_info = {a: _load_json(out / f"arch.{a}.json") or {} for a in platforms}
    tests = {a: _load_json(out / f"test.{a}.json") for a in platforms}
    primary_arch = "amd64" if "amd64" in platforms else platforms[0]
    amd = arch_info.get(primary_arch) or {}

    # One record per platform. A platform whose arch job never produced arch.<arch>.json (build failed or
    # was cancelled) is recorded with result failure and no digest, which the schema allows.
    plat_records = []
    for a in platforms:
        info = arch_info[a]
        if not info.get("digest"):
            plat_records.append({"arch": a, "result": "failure"})
            continue
        test = tests.get(a) or {}
        result = "success" if (test.get("success") or test.get("skipped")) or not tests.get(a) else "failure"
        plat_records.append(
            _prune({"arch": a, "result": result, "digest": info["digest"], "size": _int(info.get("size")), "uncompressed_size": _int(info.get("uncompressed_size")) or None, "layers": _int(info.get("layers")) or None, "runner": info.get("runner"), "build_seconds": info.get("build_seconds")})
        )

    test_enabled = str(prepare.get("test_enabled", "true")).lower() != "false"
    per_arch = {a: bool(tests[a].get("success")) for a in platforms if tests.get(a)}
    test_success = bool(per_arch) and all(per_arch.values()) and len(per_arch) == len(platforms)
    if not test_enabled:
        test_success = True
    test_block: dict[str, Any] = {"enabled": test_enabled, "success": test_success}
    if per_arch:
        test_block["per_arch"] = per_arch
    # Results are rendered by the hub from the Release assets (CONVENTIONS §10): live builds get the
    # deterministic hub page; dev and pr builds have no release, so they link to the control plane's run page.
    if mode == "live" and (site_url or hub_url):
        # The website renders the CI results from the hub's data (CONVENTIONS §10); the hub page is the fallback.
        base = (site_url.rstrip("/") + "/images") if site_url else (hub_url.rstrip("/") + "/builds")
        test_block["report_url"] = f"{base}/{prepare['name']}/builds/{prepare['primary_tag']}/" if site_url else f"{base}/{prepare['name']}/{prepare['primary_tag']}/"
    elif control_url:
        test_block["report_url"] = f"{control_url.rstrip('/')}/builds/{repository}/{run_id}"

    assets = []
    for a in platforms:
        for pattern in ASSET_PATTERNS:
            p = out / pattern.format(a=a)
            if p.is_file():
                assets.append({"name": p.name, "size": p.stat().st_size, "sha256": _sha256(p)})

    semver = prepare.get("semver") or ""
    version = _prune(
        {
            "upstream": prepare.get("upstream", ""),
            "upstream_clean": prepare.get("upstream_clean", ""),
            "semver": semver or None,
            "release_tag": prepare.get("release_tag"),
            "tags": json.loads(prepare["all_tags"]) if prepare.get("all_tags") else None,
            "source": prepare.get("source"),
            "source_url": prepare.get("source_url"),
        }
    )
    version["ls_build"] = _int(prepare.get("ls_build")) if mode == "live" else 0
    version["tag"] = prepare.get("primary_tag", "")

    if mode == "pr":
        reason = "pull_request"
    if reason not in REASONS:
        reason = "manual"

    base_digest = amd.get("base_digest") or ""
    bundle: dict[str, Any] = {
        "schema": 1,
        "image": prepare["name"],
        "repository": repository,
        "branch": prepare.get("branch", ""),
        "mode": mode,
        "version": version,
        "commit": prepare["sha"],
        "created": created or dt.datetime.now(tz=dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reason": reason,
        "base": _prune({"family": prepare.get("base", "other"), "image": amd.get("base_image") or prepare.get("base_image") or "unknown", "digest": base_digest if SHA256_RE.match(base_digest) else None}),
        "platforms": plat_records,
        "labels": amd.get("labels") or {},
        "config": _image_config(_load_json(out / f"image-config.{primary_arch}.json")),
        "dockerfile": {a: arch_info[a].get("dockerfile") or (out / f"dockerfile.{a}").read_text(encoding="utf-8") if (out / f"dockerfile.{a}").is_file() else arch_info[a].get("dockerfile", "") for a in platforms},
        "test": test_block,
        "assets": assets,
        "workflow": _prune({"run_id": run_id, "run_url": run_url, "workflows_ref": workflows_ref, "ci_version": ci_image.rsplit(":", 1)[-1] if ":" in ci_image else None}),
    }
    bundle["dockerfile"] = {a: t for a, t in bundle["dockerfile"].items() if t} or {"amd64": ""}
    if "amd64" not in bundle["dockerfile"]:
        bundle["dockerfile"]["amd64"] = ""
    if mode == "pr" and prepare.get("pr_number"):
        bundle["pull_request"] = _int(prepare["pr_number"])
    if prepare.get("previous_tag"):
        bundle["previous"] = _prune({"tag": prepare["previous_tag"], "release_url": prepare.get("previous_release_url")})
    if index_digest and SHA256_RE.match(index_digest):
        bundle["index_digest"] = index_digest
    graph = _init_graph(_load_json(out / f"init-graph.{primary_arch}.json"))
    if graph:
        bundle["init_graph"] = graph
    packages = {a: s for a in platforms if (s := _packages_summary(out, a))}
    if packages:
        bundle["packages"] = packages
    return bundle


def validate(bundle: dict[str, Any]) -> list[str]:
    """Validate with the contracts package when available; [] when valid or unavailable."""
    try:
        import contracts  # noqa: PLC0415
    except ImportError:
        gha.warning("contracts package not importable; skipping bundle validation")
        return []
    return [contracts.format_error(e) for e in contracts.errors("bundle", bundle)]


def summary_markdown(bundle: dict[str, Any]) -> str:
    v = bundle["version"]
    lines = [
        f"## Build bundle: `{bundle['image']}` `{v.get('tag')}`",
        "",
        f"- mode `{bundle['mode']}`, branch `{bundle['branch']}`, reason `{bundle['reason']}`",
        f"- upstream `{v.get('upstream')}` (source `{v.get('source', '')}`), ls build {v.get('ls_build')}",
        f"- base `{bundle['base'].get('image')}` `{bundle['base'].get('digest', '')}`",
        f"- tests: {'PASS' if bundle['test']['success'] else 'FAIL'} " + " ".join(f"`{a}`={'ok' if ok else 'fail'}" for a, ok in (bundle["test"].get("per_arch") or {}).items()),
    ]
    if bundle.get("index_digest"):
        lines.append(f"- index digest `{bundle['index_digest']}`")
    for p in bundle["platforms"]:
        lines.append(f"- `{p['arch']}`: {p['result']} `{p.get('digest', '')}` {(p.get('size') or 0) // 1024 // 1024} MiB compressed, {p.get('layers', '?')} layers")
    for a, s in (bundle.get("packages") or {}).items():
        lines.append(f"- packages `{a}`: {s['count']} ({s['distro']}), hash `{s['hash']}`")
    lines.append(f"- assets: {', '.join(a['name'] for a in bundle['assets']) or 'none'}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--prepare", required=True, help="JSON file with the prepare job outputs")
    ap.add_argument("--repository", required=True)
    ap.add_argument("--run-id", required=True, type=int)
    ap.add_argument("--run-url", required=True)
    ap.add_argument("--workflows-ref", required=True)
    ap.add_argument("--ci-image", default="")
    ap.add_argument("--reason", default="push")
    ap.add_argument("--index-digest", default="")
    ap.add_argument("--control-url", default="")
    ap.add_argument("--hub-url", default="")
    ap.add_argument("--site-url", default="")
    ap.add_argument("--output", default="", help="where to write build.json (default: <out>/build.json)")
    args = ap.parse_args(argv)

    out = Path(args.out)
    prepare = json.loads(Path(args.prepare).read_text(encoding="utf-8"))
    bundle = build_bundle(prepare, out, repository=args.repository, run_id=args.run_id, run_url=args.run_url, workflows_ref=args.workflows_ref, ci_image=args.ci_image, reason=args.reason, index_digest=args.index_digest, control_url=args.control_url, hub_url=args.hub_url, site_url=args.site_url)
    target = Path(args.output) if args.output else out / "build.json"
    target.write_text(json.dumps(bundle, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    problems = validate(bundle)
    for p in problems:
        gha.error(f"build.json: {p}")
    gha.set_output("bundle", str(target))
    gha.set_output("test_success", bundle["test"]["success"])
    gha.set_output("primary_tag", bundle["version"].get("tag", ""))
    gha.summary(summary_markdown(bundle))
    print(f"wrote {target} ({'valid' if not problems else f'{len(problems)} problem(s)'})")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
