#!/usr/bin/env bash
# wait.sh <seconds>: before another Pages deploy; 0 when the last one worked but a release came meanwhile, so nothing
# was refused and there is nothing to wait for. The usual refusal is a deploy of another run in progress (two
# branches of one repository released together), so wait for it; a repository without Pages is not worth a retry.
set -euo pipefail
# shellcheck source=../../../scripts/lib.sh
source "${GITHUB_ACTION_PATH:-$(dirname "${BASH_SOURCE[0]}")}/../../../scripts/lib.sh"

if [[ "${1}" == "0" ]]; then
  echo "A release came while this site was assembled; assembling again"
  exit 0
fi

if ! build_type=$(retry gh api "repos/${REPOSITORY}/pages" --jq .build_type 2>/dev/null); then
  die "Pages is not enabled on ${REPOSITORY} (Settings, Pages, Source: GitHub Actions); not retrying"
fi
[[ "${build_type}" == "workflow" ]] || die "Pages on ${REPOSITORY} deploys from a branch, not from GitHub Actions; not retrying"
echo "Pages deploy refused; waiting ${1}s for the deploy in progress, then assembling again"
sleep "${1}"
