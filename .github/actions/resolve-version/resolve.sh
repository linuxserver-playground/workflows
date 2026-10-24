#!/usr/bin/env bash
# Run scripts/resolve_version.py against the manifest and turn its JSON into step outputs.
set -euo pipefail
# shellcheck source=../../../scripts/lib.sh
source "${GITHUB_ACTION_PATH}/../../../scripts/lib.sh"

manifest="${1:?manifest path}"
[[ -f "${manifest}" ]] || die "manifest ${manifest} not found"

group "Resolving upstream version"
if ! result=$(python3 "${GITHUB_ACTION_PATH}/../../../scripts/resolve_version.py" "${manifest}"); then
  rc=$?
  if [[ "${rc}" == "3" ]]; then
    die "upstream assets are not published yet; try again later"
  fi
  die "could not resolve the upstream version (exit ${rc})"
fi
echo "${result}"
endgroup

for key in upstream upstream_clean semver source source_url; do
  value=$(jq -r --arg k "${key}" '.[$k] // ""' <<< "${result}")
  set_output "${key}" "${value}"
done
summary "Upstream version \`$(jq -r .upstream <<< "${result}")\` (source \`$(jq -r .source <<< "${result}")\`)"
