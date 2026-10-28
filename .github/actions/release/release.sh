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
SITE_URL="${SITE_URL:-}"
[[ -n "${SITE_URL}" ]] || die "SITE_URL is not set; the release notes link the build's CI results page on the website"
RUN_URL="${RUN_URL:-}"
work="${RUNNER_TEMP:-/tmp}/lsio-release"
mkdir -p "${work}"

[[ -f "${BUNDLE_DIR}/build.json" ]] || die "${BUNDLE_DIR}/build.json missing; emit-bundle must run first"

# ---- Does the release already exist (re-run of the same build)? ----
exists=false
# gh says "release not found" for a missing one, which retry returns at once.
if retry gh release view "${TAG}" --repo "${REPOSITORY}" --json url >/dev/null 2>&1; then
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
  if ! retry gh api --method POST "repos/${REPOSITORY}/releases/generate-notes" "${gen_args[@]}" --jq .body > "${work}/linuxserver.md"; then
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

# ---- Section 1: CI links: the build's page on the website (CONVENTIONS §10), control live status ----
ci_url="${SITE_URL%/}/images/${IMAGE_NAME}/builds/${TAG}/"
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

# ---- Tag and release: the tag at the sha, the release on the branch (CONVENTIONS §3) ----
if [[ "${exists}" == "false" ]]; then
  group "Creating tag ${TAG} at ${SHA} and release on ${BRANCH}"
  "${GITHUB_ACTION_PATH}/create_release.sh" "${TAG}" "${SHA}" "${BRANCH}" "${REPOSITORY}" "${work}/body.md" "${PRERELEASE}"
  endgroup
fi

# ---- Assets (CONVENTIONS §6) ----
group "Uploading bundle assets"
assets=("${BUNDLE_DIR}/build.json")
for f in "${BUNDLE_DIR}"/packages.*.json.gz "${BUNDLE_DIR}"/layers.*.json "${BUNDLE_DIR}"/files.*.json.gz \
         "${BUNDLE_DIR}"/test.*.json "${BUNDLE_DIR}"/test.*.tar.gz "${BUNDLE_DIR}"/package_versions.*.txt \
         "${BUNDLE_DIR}"/record.json; do
  [[ -f "${f}" ]] && assets+=("${f}")
done
retry gh release upload "${TAG}" "${assets[@]}" --repo "${REPOSITORY}" --clobber
endgroup

release_url=$(retry gh release view "${TAG}" --repo "${REPOSITORY}" --json url --jq .url)
set_output release_url "${release_url}"
set_output created "$([[ "${exists}" == "false" ]] && echo true || echo false)"
summary "### Release
[${TAG}](${release_url}) $([[ "${exists}" == "false" ]] && echo created || echo updated) with ${#assets[@]} assets"
