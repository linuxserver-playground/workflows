#!/usr/bin/env python3
"""
The version a mod build passes as MOD_VERSION and the extra tags it publishes (CONVENTIONS §3, "Mods").

    mod_version.py <mod.yml> [--version V] [--line NAME] [--output FILE]

`upstream.version` in mod.yml is the default line; `upstream.lines` adds named ones. The line to build is
`--line` (empty: the default line). Its version is `--version` when given (the control plane resolved it already),
else the line's source resolved with resolve_version. Appends to FILE (GITHUB_OUTPUT), or prints:

    mod_version=<the version as resolved>      empty when the mod has no upstream block or it did not resolve
    version_tag=<the version without the characters a tag cannot hold>
    line=<the line's name, empty for the default line>
    aliases=<JSON list of the line's floating tag suffixes: `major` X and `minor` X.Y of X.Y.Z>

A line name mod.yml does not have exits 2; an unresolved source only warns, as the Dockerfile's own lookup then
applies.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import resolve_version as rv  # noqa: E402


def lines_of(mod: dict[str, Any]) -> list[dict[str, Any]]:
    """The default line (name "") then mod.yml's `upstream.lines`, each with `version` and `tags`."""
    upstream = mod.get("upstream") or {}
    if not upstream.get("version"):
        return []
    lines = [{"name": "", "version": upstream["version"], "tags": upstream.get("tags") or []}]
    lines += [
        {"name": str(line["name"]), "version": line["version"], "tags": line.get("tags") or []}
        for line in upstream.get("lines") or []
    ]
    return lines


def alias_suffixes(version_tag: str, kinds: list[str]) -> list[str]:
    """`major` is the version up to its first dot, `minor` up to its second; no dot, no such tag. Same rule as
    builder.mods.line_tag_suffixes."""
    parts = version_tag.split(".")
    out = []
    if "major" in kinds and len(parts) >= 2:
        out.append(parts[0])
    if "minor" in kinds and len(parts) >= 3:
        out.append(".".join(parts[:2]))
    return [s for s in out if s and s != version_tag]


def plan(mod: dict[str, Any], version: str = "", line: str = "") -> dict[str, Any]:
    lines = lines_of(mod)
    if line and line not in {entry["name"] for entry in lines}:
        raise KeyError(line)
    chosen = next((entry for entry in lines if entry["name"] == line), None)
    if not version and chosen is not None:
        manifest = {
            "upstream": {"version": chosen["version"]},
            "build": {"platforms": mod.get("platforms") or ["amd64", "arm64"]},
        }
        try:
            result = rv.resolve(manifest)
        except rv.ResolveError as exc:
            print(f"::warning file=mod.yml::upstream version not resolved ({exc}); building without MOD_VERSION")
        else:
            version = str(result["upstream"])
            print(f"MOD_VERSION {version} from {result['source_url']}", file=sys.stderr)
    elif version:
        print(f"MOD_VERSION {version} (input)", file=sys.stderr)
    else:
        print("no upstream block in mod.yml; the Dockerfile's own lookup applies", file=sys.stderr)
    tag = rv.sanitize(version)
    return {
        "mod_version": version,
        "version_tag": tag,
        "line": line,
        "aliases": alias_suffixes(tag, list(chosen["tags"])) if chosen and tag else [],
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("mod_yml")
    parser.add_argument("--version", default="")
    parser.add_argument("--line", default="")
    parser.add_argument("--output", help="append the key=value lines here (GITHUB_OUTPUT) instead of stdout")
    args = parser.parse_args(argv)
    import yaml

    mod = yaml.safe_load(Path(args.mod_yml).read_text(encoding="utf-8")) or {}
    try:
        result = plan(mod, args.version.strip(), args.line.strip())
    except KeyError:
        print(f"::error file=mod.yml::no version line named {args.line!r} in upstream.lines")
        return 2
    text = "".join(f"{k}={json.dumps(v) if isinstance(v, list) else v}\n" for k, v in result.items())
    if args.output:
        with open(args.output, "a", encoding="utf-8") as out:
            out.write(text)
    print(text, end="", file=sys.stderr if args.output else sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
