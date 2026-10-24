#!/usr/bin/env bash
# Create the git tag and GitHub Release for a live build and attach the bundle.
set -euo pipefail
# shellcheck source=../../../scripts/lib.sh
source "${GITHUB_ACTION_PATH}/../../../scripts/lib.sh"
scripts="${GITHUB_ACTION_PATH}/../../../scripts"

: "${TAG:?}" "${SHA:?}" "${BRANCH:?}" "${REPOSITORY:?}" "${BUNDLE_DIR:?}" "${MANIFEST:?}" "${UPSTREAM:?}"
PRERELEASE="${PRERELEASE:-false}"
PREVIOUS_TAG="${PREVIOUS_TAG:-}"
HEAD_ALREADY_RELEASED="${HEAD_ALREADY_RELEASED:-false}"
CONTROL_URL="${CONTROL_URL:-}"
IMAGE_NAME="${IMAGE_NAME:?}"
RUN_URL="${RUN_URL:-}"
work="${RUNNER_TEMP:-/tmp}/lsio-release"
mkdir -p "${work}"

[[ -f "${BUNDLE_DIR}/build.json" ]] || die "${BUNDLE_DIR}/build.json missing; emit-bundle must run first"

# ---- Does the release already exist (re-run of the same build)? ----
exists=false
if gh release view "${TAG}" --repo "${REPOSITORY}" --json url >/dev/null 2>&1; then
  exists=true
  notice "release ${TAG} already exists; assets will be re-uploaded with --clobber"
fi

# ---- Section 2: LinuxServer changes (GitHub generated notes) ----
group "Generating LinuxServer release notes"
if [[ "${HEAD_ALREADY_RELEASED}" == "true" && -n "${PREVIOUS_TAG}" ]]; then
  echo "No changes" > "${work}/linuxserver.md"
else
  gen_args=(-f "tag_name=${TAG}" -f "target_commitish=${BRANCH}")
  [[ -n "${PREVIOUS_TAG}" ]] && gen_args+=(-f "previous_tag_name=${PREVIOUS_TAG}")
  if ! retry 3 10 gh api --method POST "repos/${REPOSITORY}/releases/generate-notes" "${gen_args[@]}" --jq .body > "${work}/linuxserver.md"; then
    warn "generate-notes failed; the LinuxServer section will be empty"
    echo "" > "${work}/linuxserver.md"
  fi
fi
endgroup

# ---- Section 3: upstream notes ----
group "Fetching upstream release notes"
if ! python3 "${scripts}/upstream_notes.py" "${MANIFEST}" "${UPSTREAM}" > "${work}/upstream.json"; then
  warn "upstream notes could not be fetched"
  echo '{"text":"","url":""}' > "${work}/upstream.json"
fi
endgroup

# ---- Section 1: CI links: hub results page (rendered from this release's assets), control live status ----
if [[ -n "${HUB_URL:-}" ]]; then
  ci_url="${HUB_URL%/}/builds/${IMAGE_NAME}/${TAG}/"
else
  ci_url=""
fi
if [[ -n "${CONTROL_URL}" ]]; then
  status_url="${CONTROL_URL%/}/builds/${REPOSITORY}/${GITHUB_RUN_ID:-0}"
else
  status_url="${RUN_URL}"
fi

python3 "${GITHUB_ACTION_PATH}/release_notes.py" compose \
  --ci-url "${ci_url}" --status-url "${status_url}" --linuxserver-notes "${work}/linuxserver.md" \
  --upstream-notes "${work}/upstream.json" --notes-out "${work}/notes.json" > "${work}/body.md"

# ---- Patch build.json with notes and asset URLs, re-validate ----
asset_base="https://github.com/${REPOSITORY}/releases/download/${TAG}"
python3 "${GITHUB_ACTION_PATH}/release_notes.py" patch-bundle "${BUNDLE_DIR}/build.json" \
  --notes "${work}/notes.json" --asset-url-base "${asset_base}"
if python3 -c 'import contracts' 2>/dev/null; then
  python3 -m contracts bundle "${BUNDLE_DIR}/build.json"
fi

# ---- Tag and release ----
if [[ "${exists}" == "false" ]]; then
  group "Creating tag and release ${TAG} at ${SHA}"
  create_args=(--repo "${REPOSITORY}" --target "${SHA}" --title "${TAG}" --notes-file "${work}/body.md")
  [[ "${PRERELEASE}" == "true" ]] && create_args+=(--prerelease)
  retry 3 15 gh release create "${TAG}" "${create_args[@]}"
  endgroup
fi

# ---- Assets (CONVENTIONS §6) ----
group "Uploading bundle assets"
assets=("${BUNDLE_DIR}/build.json")
for f in "${BUNDLE_DIR}"/packages.*.json.gz "${BUNDLE_DIR}"/layers.*.json "${BUNDLE_DIR}"/files.*.json.gz \
         "${BUNDLE_DIR}"/test.*.json "${BUNDLE_DIR}"/test.*.tar.gz "${BUNDLE_DIR}"/package_versions.*.txt; do
  [[ -f "${f}" ]] && assets+=("${f}")
done
retry 3 15 gh release upload "${TAG}" "${assets[@]}" --repo "${REPOSITORY}" --clobber
endgroup

release_url=$(gh release view "${TAG}" --repo "${REPOSITORY}" --json url --jq .url)
set_output release_url "${release_url}"
set_output created "$([[ "${exists}" == "false" ]] && echo true || echo false)"
summary "### Release
[${TAG}](${release_url}) $([[ "${exists}" == "false" ]] && echo created || echo updated) with ${#assets[@]} assets"
