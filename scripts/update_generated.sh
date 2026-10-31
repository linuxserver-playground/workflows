#!/usr/bin/env bash
# Templating step of the prepare job.
#
#   update_generated.sh render <builder image> <branch>   live: render, commit once, push, continue
#   update_generated.sh check  <builder image>            pr/dev: validate; drift only warns
#
# A PR or branch build never re-renders: the live build does that once after merge. Generated files that
# differ from a fresh render there only mean the builder changed since the live branch was templated, so
# 'check' reports that as a warning and fails on real problems only (image.yml, Dockerfiles, s6 scripts,
# credentials). With BASE_SHA (a pull request's base) it also fails when the PR itself edits a generated
# file, since those are never edited by hand.
#
# The builder container is run with the repository mounted at /repo (its entrypoint cds there and
# runs the builder CLI). 'render' is limited to the in-repo files so .builder-out/ is never written
# into a live checkout. root/donate.txt is rendered only when image.yml lists sponsors and removed
# otherwise; a removal shows up in the status like any other change and is committed the same way.
#
# The commit subject ends in "[skip ci]": the run that made the commit continues on it, so a second
# run of the same workflow for that push would only build the same tree again (and, when HEAD was
# already released, overwrite the same -lsN). GitHub skips push-triggered workflows for commits whose
# message carries [skip ci]. The other bot commit, "Update package versions [skip ci]" (package-check,
# after a release), carries it for the same reason. Outputs: checkout_sha (the commit the arch jobs
# must build) and generated_updated (true|false).
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

cmd="${1:?render|check}"
builder_image="${2:-}"
branch="${3:-}"
GENERATED_FILES=(README.md AGENTS.md LICENSE .github/FUNDING.yml root/donate.txt .github/workflows/build.yml .github/workflows/pr-report.yml)
COMMIT_SUBJECT="Update generated files [skip ci]"
head_sha=$(git rev-parse HEAD)

if [[ -z "${builder_image}" ]]; then
  notice "builder_image is empty; skipping generated-file ${cmd}"
  set_output checkout_sha "${head_sha}"
  set_output generated_updated false
  exit 0
fi

group "Pulling ${builder_image}"
retry_ghcr docker pull "${builder_image}"
endgroup

run_builder() {
  docker run --rm \
    -v "${PWD}:/repo" \
    -e GITHUB_TOKEN="${GITHUB_TOKEN:-}" \
    -e GITHUB_REPOSITORY="${GITHUB_REPOSITORY:-}" \
    --user "$(id -u):$(id -g)" \
    "${builder_image}" "$@"
}

if [[ "${cmd}" == "check" ]]; then
  if [[ -n "${BASE_SHA:-}" ]]; then
    base=$(git merge-base "${BASE_SHA}" HEAD 2>/dev/null || echo "${BASE_SHA}")
    edited=$(git diff --name-only "${base}" HEAD -- "${GENERATED_FILES[@]}")
    if [[ -n "${edited}" ]]; then
      die "this pull request edits generated files ($(echo "${edited}" | paste -sd, -)); change image.yml or the builder templates instead, the live build renders them after merge"
    fi
  fi
  group "builder check"
  out=$(run_builder check --drift warn 2>&1) && rc=0 || rc=$?
  echo "${out}"
  endgroup
  if (( rc != 0 )); then
    die "builder check failed: image.yml or the repository has problems (listed above)."
  fi
  drifted=$(grep -E '^warning: [^:]+: <file>:' <<< "${out}" | sed -E 's/^warning: ([^:]+):.*/\1/' | paste -sd, - || true)
  if [[ -n "${drifted}" ]]; then
    warn "generated files differ from the current builder (${drifted}); the live build re-renders them after merge"
  fi
  set_output checkout_sha "${head_sha}"
  set_output generated_updated false
  exit 0
fi

[[ "${cmd}" == "render" ]] || die "unknown command ${cmd}"
[[ -n "${branch}" ]] || die "render needs the branch name"

group "builder render"
run_builder render --only "$(IFS=,; echo "${GENERATED_FILES[*]}")"
endgroup

changed=$(git status --porcelain -- "${GENERATED_FILES[@]}" | awk '{print $2}')
if [[ -z "${changed}" ]]; then
  echo "Generated files are current."
  set_output checkout_sha "${head_sha}"
  set_output generated_updated false
  exit 0
fi

echo "Generated files changed:"
echo "${changed}"
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

# Rebase onto the branch and push, as one unit: a push refused because the branch moved meanwhile is tried again
# from a new rebase (RETRY_ALSO), and network failures of either half are retried. Anything else, a rebase conflict
# or a refused workflow-file push, returns at once.
pull_and_push() {
  local rc=0
  git pull --rebase --quiet origin "${branch}" && git push --quiet origin "HEAD:${branch}" || rc=$?
  (( rc == 0 )) || git rebase --abort 2>/dev/null || true
  return "${rc}"
}

commit_and_push() {
  # $@ = files to include
  git add -- "$@"
  git commit --quiet -m "${COMMIT_SUBJECT}"
  RETRY_ALSO='\[rejected\]|non-fast-forward|fetch first' retry pull_and_push
}

# shellcheck disable=SC2086
if commit_and_push ${changed}; then
  :
elif grep -q '^.github/workflows/' <<< "${changed}"; then
  # GITHUB_TOKEN may not update workflow files (it lacks the 'workflows' permission). Commit the
  # rest and leave the workflows for the control plane's GitHub App to update.
  workflows=$(grep '^.github/workflows/' <<< "${changed}")
  warn "could not push $(paste -sd, - <<< "${workflows}") with GITHUB_TOKEN; committing the other generated files only. The control plane must update the workflow files."
  git reset --quiet --soft "${head_sha}"
  # shellcheck disable=SC2086
  git restore --staged --worktree -- ${workflows}
  rest=$(grep -v '^.github/workflows/' <<< "${changed}" || true)
  if [[ -n "${rest}" ]]; then
    # shellcheck disable=SC2086
    commit_and_push ${rest}
  else
    set_output checkout_sha "${head_sha}"
    set_output generated_updated false
    exit 0
  fi
else
  die "could not push the generated files"
fi

new_sha=$(git rev-parse HEAD)
notice "Committed generated files as ${new_sha}; the build continues on that commit."
set_output checkout_sha "${new_sha}"
set_output generated_updated true
