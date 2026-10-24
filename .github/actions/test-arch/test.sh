#!/usr/bin/env bash
# Run the ci harness. The interface is fixed (README "CI container interface"): the container
# exits 0 even when tests fail and says so in test.<arch>.json; non-zero means the harness broke.
set -euo pipefail
# shellcheck source=../../../scripts/lib.sh
source "${GITHUB_ACTION_PATH}/../../../scripts/lib.sh"

: "${IMAGE:?}" "${ARCH:?}" "${MODE:?}" "${MANIFEST_JSON:?}" "${OUT_DIR:?}"
CI_IMAGE="${CI_IMAGE:-ghcr.io/linuxserver-playground/ci:latest}"
mkdir -p "${OUT_DIR}"
out_abs=$(cd "${OUT_DIR}" && pwd)

group "Pulling ${CI_IMAGE}"
retry_ghcr docker pull "${CI_IMAGE}"
endgroup

group "Running ci harness for ${IMAGE} (${ARCH}, ${MODE})"
start=${SECONDS}
set +e
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "${out_abs}":/out \
  -e IMAGE="${IMAGE}" \
  -e ARCH="${ARCH}" \
  -e MODE="${MODE}" \
  -e MANIFEST_JSON="${MANIFEST_JSON}" \
  -e PREVIOUS_PACKAGES_URL="${PREVIOUS_PACKAGES_URL:-}" \
  "${CI_IMAGE}"
rc=$?
set -e
echo "ci harness exited ${rc} after $(( SECONDS - start ))s"
endgroup

sudo chown -R "$(id -u):$(id -g)" "${out_abs}" 2>/dev/null || true
ls -la "${out_abs}" || true

report="${out_abs}/test.${ARCH}.json"
tests_enabled=$(jq -r '.test.enabled // true' <<< "${MANIFEST_JSON}")
success=false
table=""
if [[ -f "${report}" ]]; then
  success=$(jq -r '.success // false' "${report}")
  table=$(jq -r '.tests[]? | "| \(.name) | \(.status) | \(.message // "" | gsub("\n"; " ") | .[0:120]) |"' "${report}")
elif [[ "${rc}" == "0" ]]; then
  error "ci harness exited 0 but wrote no ${report}"
fi
if [[ "${tests_enabled}" == "false" ]]; then
  notice "tests are disabled in image.yml; recording success=true for ${ARCH}"
  success=true
fi

set_output success "${success}"
set_output report "${report}"
set_output summary "${table}"
{
  echo "### Tests (${ARCH}): $([[ "${success}" == "true" ]] && echo PASS || echo FAIL)"
  echo
  if [[ -n "${table}" ]]; then
    echo "| Test | Status | Message |"
    echo "| --- | --- | --- |"
    echo "${table}"
  fi
} >> "${GITHUB_STEP_SUMMARY:-/dev/stdout}"

if [[ "${rc}" != "0" ]]; then
  die "ci harness itself failed (exit ${rc})"
fi
