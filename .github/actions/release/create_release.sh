#!/usr/bin/env bash
# Create the git tag at the built commit, then the GitHub Release on the branch.
#
#   create_release.sh <tag> <sha> <branch> <repository> <notes file> [prerelease true|false]
#
# CONVENTIONS §3: the Release's target_commitish is always the branch name, never a sha, so a
# branch's releases can be listed without parsing tag prefixes. GitHub only honours target_commitish
# as the commit to tag when the tag does not exist yet, so the tag is created first, explicitly at the
# built sha through the refs API, and the release then names the branch. An existing tag is accepted
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
if existing=$(gh api "repos/${repository}/git/ref/tags/${tag}" --jq '.object.type + " " + .object.sha' 2>/dev/null); then
  obj_type="${existing%% *}"
  obj_sha="${existing#* }"
  if [[ "${obj_type}" == "tag" ]]; then
    # Annotated tag: resolve the tag object to the commit it points at.
    obj_sha=$(gh api "repos/${repository}/git/tags/${obj_sha}" --jq '.object.sha')
  fi
  [[ "${obj_sha}" == "${sha}" ]] || die "tag ${tag} already exists at ${obj_sha}, not at ${sha}; a release tag never moves"
  notice "tag ${tag} already points at ${sha}"
else
  retry 3 10 gh api --method POST "repos/${repository}/git/refs" -f "ref=refs/tags/${tag}" -f "sha=${sha}" >/dev/null
  echo "created tag ${tag} at ${sha}"
fi

# ---- Release on the branch ----
create_args=(--repo "${repository}" --target "${branch}" --title "${tag}" --notes-file "${notes_file}")
[[ "${prerelease}" == "true" ]] && create_args+=(--prerelease)
retry 3 15 gh release create "${tag}" "${create_args[@]}"
