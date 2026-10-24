# AGENTS.md - workflows

## Purpose

This repo is the shared GitHub Actions build system for every `docker-<name>` repository and every
`docker-mods` branch in `linuxserver-playground`. Image repos call `image-build.yml@v1` from a generated
20-line caller; everything the build does (version resolution, build, test, publish, release, control-plane
event) lives here. Consumers are every image repo, the control plane (which imports
`scripts/resolve_version.py` and receives `build.summary` events) and the hub (which ingests the `build.json`
bundle attached to each Release). The contract is `CONVENTIONS.md` in `linuxserver-playground/builder`.

## Layout

| Path | What |
| --- | --- |
| `.github/workflows/image-build.yml` | The reusable image build: jobs `prepare`, `arch` (matrix), `publish`, and `cleanup` (closed pull requests only). |
| `.github/workflows/mod-build.yml` | Reusable mod build for `docker-mods` branches. |
| `.github/workflows/cleanup-staging.yml` | Scheduled deletion of old `build/*` staging images (needs `PACKAGES_TOKEN`). |
| `.github/workflows/ci.yml` | This repo's own checks. |
| `.github/actions/<name>/` | One composite action per step; logic in the script next to `action.yml`. `build-arch` pushes to `build/` staging refs, `publish-manifest` creates the released tags from them, `package-check` is the `reason: packages` comparison and commit. |
| `scripts/` | `lib.sh` bash helpers; Python modules shared by actions and tests. |
| `templates/build.yml` | The caller file the builder renders into image repos. |
| `fixtures/example-image/` | Minimal valid image repo for integration runs. |
| `tests/` | pytest; HTTP is mocked, nothing needs network or docker. |

## What you may edit

Everything, by pull request. Keep the shape: an action's `action.yml` only declares inputs, outputs and
calls its script; bash scripts start with `set -euo pipefail` and source `scripts/lib.sh`; Python is stdlib
only (PyYAML is installed in the step when needed, the contracts package comes from the builder repo).
`scripts/resolve_version.py` must stay importable without GitHub Actions context because the control plane
uses it. Third-party actions are pinned by major tag and listed in README.md (a test enforces both). Nothing
may reference a registry or namespace outside `linuxserver-playground` other than
`ghcr.io/linuxserver/baseimage-*`. Released tags are only ever written by `publish-manifest`, after every
arch job succeeded; anything that runs earlier pushes to the `build/` staging namespace. The two bot commits
are `Update generated files [skip ci]` and `Update package versions` (no `[skip ci]`: its push is the release
build); do not add a third.

Behaviour changes that touch the ci container interface, the bundle or the control event are contract
changes: coordinate with the `ci`, `hub` and `control` repos and the schemas in `builder`.

## How to validate

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
./scripts/check.sh
```

That runs shellcheck over every script, actionlint over the workflows, pytest, and validates the fixture
manifest against the image schema. CI runs the same script.

## Contracts

Consumes `image.schema.json` (reads `image.yml`), produces `bundle.schema.json` (`build.json`, validated with
`python -m contracts bundle` before upload) and `control-event.schema.json` (`build.summary`). Reads
`test-report.schema.json` output written by the ci container. All from `linuxserver-playground/builder`.

## Pull request rules

Disclose AI use in the description. One change per PR. Commit subjects are short imperative sentences
without prefixes, emoji or trailers. Do not commit credentials or generated output. A change to inputs,
outputs or the ci interface that breaks callers means a new major tag, not a change to `v1`.
