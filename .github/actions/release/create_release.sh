#!/usr/bin/env bash
# Create the git tag at the built commit, then the GitHub Release on the branch.
#
#   create_release.sh <tag> <sha> <branch> <repository> <notes file> [prerelease true|false]
#
# CONVENTIONS §3: the Release's target_commitish is always the branch name, never a sha, so a
# branch's releases can be listed without parsing tag prefixes. GitHub only honours target_commitish
# as the commit to tag when the tag does not exist yet, so the tag is created first, explicitly at the
# built sha with git (see below), and the release then names the branch. An existing tag is accepted
# when it points at the same commit (a re-run) and refused otherwise: a release tag never moves.
set -euo pipefail
# shellcheck source=../../../scripts/lib.sh
source "${GITHUB_ACTION_PATH}/../../../scripts/lib.sh"

tag="${1:?tag}"
sha="${2:?sha}"
branch="${3:?branch}"
repository="${4:?repository}"
notes_file="${5:?notes file}"
prerelease="${6:-false}"

[[ "${sha}" =~ ^[0-9a-f]{40}$ ]] || die "sha ${sha} is not a full commit id"

# ---- Tag at the built commit ----
if existing=$(retry gh api "repos/${repository}/git/ref/tags/${tag}" --jq '.object.type + " " + .object.sha' 2>/dev/null); then
  obj_type="${existing%% *}"
  obj_sha="${existing#* }"
  if [[ "${obj_type}" == "tag" ]]; then
    # Annotated tag: resolve the tag object to the commit it points at.
    obj_sha=$(retry gh api "repos/${repository}/git/tags/${obj_sha}" --jq '.object.sha')
  fi
  [[ "${obj_sha}" == "${sha}" ]] || die "tag ${tag} already exists at ${obj_sha}, not at ${sha}; a release tag never moves"
  notice "tag ${tag} already points at ${sha}"
else
  # The refs API answers 403 "Resource not accessible by integration" to the workflow token even with
  # contents: write, while a plain git push with the same token works (the generated-files commit
  # relies on it). So the tag is pushed from a minimal fetch of the built commit.
  remote="${RELEASE_GIT_REMOTE:-https://x-access-token:${GITHUB_TOKEN:?GITHUB_TOKEN}@github.com/${repository}.git}"
  work=$(mktemp -d)
  git init --quiet "${work}"
  git -C "${work}" remote add origin "${remote}"
  retry git -C "${work}" fetch --quiet --depth 1 origin "${sha}"
  git -C "${work}" tag "${tag}" "${sha}"
  retry git -C "${work}" push --quiet origin "refs/tags/${tag}"
  rm -rf "${work}"
  echo "created tag ${tag} at ${sha}"
fi

# ---- Release on the branch ----
create_args=(--repo "${repository}" --target "${branch}" --title "${tag}" --notes-file "${notes_file}")
[[ "${prerelease}" == "true" ]] && create_args+=(--prerelease)
# A create whose response was lost leaves the release made; the retry is then refused as a duplicate. It is ours: the
# caller only gets here when the release did not exist.
if ! retry gh release create "${tag}" "${create_args[@]}"; then
  retry gh release view "${tag}" --repo "${repository}" --json url >/dev/null 2>&1 || die "could not create the release ${tag}"
  notice "release ${tag} exists after a failed create; keeping it"
fi
