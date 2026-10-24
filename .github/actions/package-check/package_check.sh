#!/usr/bin/env bash
# The reason=packages check (Jenkins "Update-packages" parity). Runs in the publish job of a live
# build after prepare, build and test succeeded; the fresh image is needed to know its packages.
#
#   unchanged  nothing is published or released; outputs changed=false. The caller emits the bundle
#              as a workflow artifact only and reports result "skipped".
#   changed    package_versions.txt (the amd64 table) is committed to the live branch as
#              "Update package versions" without [skip ci]: that push is what starts the normal
#              release build, which gets the next -lsN because HEAD moved. Nothing is published or
#              released from this run. A push that fails after the retries fails the run.
#   missing    no package_versions.txt in the repo yet: treated as changed, the first run seeds it.
#
# The comparison is an md5 over the table with trailing whitespace normalised
# (scripts/package_versions.py). arm64 is compared against the previous release's
# package_versions.arm64.txt asset for reporting only.
set -euo pipefail
# shellcheck source=../../../scripts/lib.sh
source "${GITHUB_ACTION_PATH}/../../../scripts/lib.sh"
scripts="${GITHUB_ACTION_PATH}/../../../scripts"

: "${OUT_DIR:?}" "${REPO_DIR:?}" "${SHA:?}" "${BRANCH:?}" "${REPOSITORY:?}"
PREVIOUS_TAG="${PREVIOUS_TAG:-}"
PUSH_ATTEMPTS="${PUSH_ATTEMPTS:-3}"
PUSH_DELAY="${PUSH_DELAY:-10}"
COMMIT_SUBJECT="Update package versions"

out_abs=$(cd "${OUT_DIR}" && pwd)
fresh="${out_abs}/package_versions.amd64.txt"
[[ -f "${fresh}" ]] || die "${fresh} missing; the ci harness did not write the amd64 package table"
work="${RUNNER_TEMP:-/tmp}/lsio-package-check"
mkdir -p "${work}"

cd "${REPO_DIR}"
git rev-parse --verify --quiet "${SHA}^{commit}" >/dev/null || die "commit ${SHA} is not in ${REPO_DIR}"

# ---- The committed table at the build sha ----
committed_args=()
if git show "${SHA}:package_versions.txt" > "${work}/committed.txt" 2>/dev/null; then
  committed_args=(--committed "${work}/committed.txt")
else
  notice "no package_versions.txt at ${SHA:0:7}; the first package check seeds it"
fi

arm64_args=()
if [[ -n "${PREVIOUS_TAG}" && -f "${out_abs}/package_versions.arm64.txt" ]]; then
  arm64_args=(--fresh-arm64 "${out_abs}/package_versions.arm64.txt" \
    --previous-arm64-url "https://github.com/${REPOSITORY}/releases/download/${PREVIOUS_TAG}/package_versions.arm64.txt")
fi

result=$(python3 "${scripts}/package_versions.py" compare --fresh "${fresh}" "${committed_args[@]}" "${arm64_args[@]}")
echo "${result}"
changed=$(jq -r '.changed' <<< "${result}")
status=$(jq -r '.status' <<< "${result}")
fresh_hash=$(jq -r '.fresh' <<< "${result}")
committed_hash=$(jq -r '.committed' <<< "${result}")
arm64_status=$(jq -r '.arm64.status // "not compared"' <<< "${result}")

set_output fresh_hash "${fresh_hash}"
set_output committed_hash "${committed_hash}"

if [[ "${changed}" != "true" ]]; then
  set_output changed false
  set_output commit_sha ""
  summary "### Package check

packages unchanged (\`${fresh_hash}\` at \`${SHA:0:7}\`); arm64 against ${PREVIOUS_TAG:-no previous release}: ${arm64_status}. Nothing published."
  exit 0
fi

# ---- Changed: commit the amd64 table to the live branch ----
group "Committing package_versions.txt (${committed_hash} -> ${fresh_hash}, ${status})"
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git checkout --quiet -B "${BRANCH}" "origin/${BRANCH}"
cp "${fresh}" package_versions.txt
git add package_versions.txt
if git diff --cached --quiet; then
  # The branch moved on and already carries this table (a concurrent run committed it).
  notice "origin/${BRANCH} already has this package table; nothing to commit"
  set_output changed false
  set_output commit_sha ""
  summary "### Package check

packages changed since \`${SHA:0:7}\` but \`${BRANCH}\` already carries the new table. Nothing published."
  endgroup
  exit 0
fi
git commit --quiet -m "${COMMIT_SUBJECT}"

# Fetch, rebase, push; retried as a unit so a commit that lands in between is rebased over, like
# update_generated.sh does for the generated files.
n=1
until git pull --rebase --quiet origin "${BRANCH}" && git push --quiet origin "HEAD:${BRANCH}"; do
  if (( n >= PUSH_ATTEMPTS )); then
    die "could not push ${COMMIT_SUBJECT} to ${BRANCH} after ${n} attempts"
  fi
  echo "push attempt ${n}/${PUSH_ATTEMPTS} failed; retrying in ${PUSH_DELAY}s" >&2
  # A rebase that stopped on a conflict (a concurrent run committed another table) must be undone
  # before the next attempt; the next pull then rebases our commit over the newer one again.
  git rebase --abort 2>/dev/null || true
  sleep "${PUSH_DELAY}"
  ((n++))
done
commit_sha=$(git rev-parse HEAD)
endgroup

# A push made with the workflow token never triggers workflows (GitHub blocks recursive runs), so the
# release build is dispatched explicitly for the branch, which now has the new table at its head.
group "Dispatching the release build for ${BRANCH}"
if gh workflow run build.yml --repo "${REPOSITORY}" --ref "${BRANCH}" -f reason=rebuild; then
  notice "Committed ${COMMIT_SUBJECT} as ${commit_sha} and dispatched the release build for ${BRANCH}."
  dispatched="dispatched"
else
  warn "Committed ${COMMIT_SUBJECT} as ${commit_sha} but could not dispatch the release build; the control plane's next check or a manual run will pick it up."
  dispatched="not dispatched (see warning)"
fi
endgroup
set_output changed true
set_output commit_sha "${commit_sha}"
summary "### Package check

packages changed (\`${committed_hash}\` -> \`${fresh_hash}\`), committed as \`${commit_sha}\`, release build ${dispatched}; arm64 against ${PREVIOUS_TAG:-no previous release}: ${arm64_status}. Nothing published from this run."
