#!/usr/bin/env bash
# The package table steps of the publish job (Jenkins "Update-packages" parity, without its second build).
#
#   package_check.sh compare   reason=packages only, before anything is published: compare the fresh amd64
#                              table with the committed package_versions.txt at the built commit. Outputs
#                              changed (true|false), status (same|different|missing), fresh_hash,
#                              committed_hash. "unchanged" means the run publishes and releases nothing;
#                              "changed" (or no table in the repo yet) means it goes on to release like any
#                              other build. arm64 is compared with the previous release's asset for the
#                              summary only.
#   package_check.sh commit    after a successful release, every live build: commit the fresh amd64 table
#                              to the live branch as "Update package versions [skip ci]" when the branch's
#                              table differs (fetch, rebase, push, retried). Outputs committed (true|false)
#                              and commit_sha. The commit starts no build: the control plane's release check
#                              counts a head that differs from the released commit only in this file as
#                              released, and the next build's record lists the commit as the pipeline's own.
#
# The comparison is an md5 over the table with trailing whitespace normalised (scripts/package_versions.py).
set -euo pipefail
# shellcheck source=../../../scripts/lib.sh
source "${GITHUB_ACTION_PATH}/../../../scripts/lib.sh"
scripts="${GITHUB_ACTION_PATH}/../../../scripts"

mode="${1:-}"
[[ "${mode}" == "compare" || "${mode}" == "commit" ]] || die "usage: package_check.sh compare|commit"
: "${OUT_DIR:?}" "${REPO_DIR:?}"
PREVIOUS_TAG="${PREVIOUS_TAG:-}"
PUSH_ATTEMPTS="${PUSH_ATTEMPTS:-3}"
PUSH_DELAY="${PUSH_DELAY:-10}"
COMMIT_SUBJECT="Update package versions [skip ci]"

out_abs=$(cd "${OUT_DIR}" && pwd)
fresh="${out_abs}/package_versions.amd64.txt"
[[ -f "${fresh}" ]] || die "${fresh} missing; the ci harness did not write the amd64 package table"
work="${RUNNER_TEMP:-/tmp}/lsio-package-check"
mkdir -p "${work}"
cd "${REPO_DIR}"

if [[ "${mode}" == "compare" ]]; then
  : "${SHA:?}" "${REPOSITORY:?}"
  git rev-parse --verify --quiet "${SHA}^{commit}" >/dev/null || die "commit ${SHA} is not in ${REPO_DIR}"

  # ---- The committed table at the build sha ----
  committed_args=()
  if git show "${SHA}:package_versions.txt" > "${work}/committed.txt" 2>/dev/null; then
    committed_args=(--committed "${work}/committed.txt")
  else
    notice "no package_versions.txt at ${SHA:0:7}; this release seeds it"
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

  set_output changed "${changed}"
  set_output status "${status}"
  set_output fresh_hash "${fresh_hash}"
  set_output committed_hash "${committed_hash}"
  if [[ "${changed}" == "true" ]]; then
    summary "### Package check

packages changed (\`${committed_hash}\` -> \`${fresh_hash}\`, ${status}) since \`${SHA:0:7}\`; arm64 against ${PREVIOUS_TAG:-no previous release}: ${arm64_status}. This run releases."
  else
    summary "### Package check

packages unchanged (\`${fresh_hash}\` at \`${SHA:0:7}\`); arm64 against ${PREVIOUS_TAG:-no previous release}: ${arm64_status}. Nothing published."
  fi
  exit 0
fi

# ---- commit: the fresh amd64 table onto the live branch, after the release ----
: "${BRANCH:?}"
set_output committed false  # until the push lands; a failure below leaves these
set_output commit_sha ""
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
retry git fetch --quiet origin "${BRANCH}" || die "could not fetch origin/${BRANCH}"
git checkout --quiet -B "${BRANCH}" "origin/${BRANCH}"
cp "${fresh}" package_versions.txt
git add package_versions.txt
if git diff --cached --quiet; then
  notice "origin/${BRANCH} already carries this package table; nothing to commit"
  summary "### Package table

\`package_versions.txt\` on \`${BRANCH}\` already matches the released image."
  exit 0
fi
group "Committing package_versions.txt to ${BRANCH}"
git commit --quiet -m "${COMMIT_SUBJECT}"

# Fetch, rebase, push; retried as a unit so a commit that lands in between is rebased over, like
# update_generated.sh does for the generated files.
n=1
until git pull --rebase --quiet origin "${BRANCH}" && git push --quiet origin "HEAD:${BRANCH}"; do
  if (( n >= PUSH_ATTEMPTS )); then
    endgroup
    summary "### Package table

\`package_versions.txt\` could not be pushed to \`${BRANCH}\` after ${n} attempts; the next release commits it."
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
notice "Committed ${COMMIT_SUBJECT} as ${commit_sha}; it starts no build."
set_output committed true
set_output commit_sha "${commit_sha}"
summary "### Package table

\`package_versions.txt\` committed to \`${BRANCH}\` as \`${commit_sha}\` (\`[skip ci]\`: no build follows)."
