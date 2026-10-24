#!/usr/bin/env bash
# docker buildx imagetools create: one index carrying every unprefixed tag, then each arch twin.
set -euo pipefail
# shellcheck source=../../../scripts/lib.sh
source "${GITHUB_ACTION_PATH}/../../../scripts/lib.sh"

: "${IMAGE_PATH:?}" "${PRIMARY_TAG:?}" "${TAGS:?}" "${PLATFORMS:?}"

mapfile -t tags < <(jq -r '.[]' <<< "${TAGS}")
mapfile -t platforms < <(jq -r '.[]' <<< "${PLATFORMS}")
(( ${#tags[@]} > 0 )) || die "no tags to publish"
(( ${#platforms[@]} > 0 )) || die "no platforms"

sources=()
for arch in "${platforms[@]}"; do
  sources+=("${IMAGE_PATH}:$(arch_prefix "${arch}")-${PRIMARY_TAG}")
done

group "Creating index ${IMAGE_PATH}:${PRIMARY_TAG} with ${#tags[@]} tags from ${sources[*]}"
targets=()
for t in "${tags[@]}"; do targets+=(-t "${IMAGE_PATH}:${t}"); done
retry_ghcr docker buildx imagetools create "${targets[@]}" "${sources[@]}"
endgroup

group "Creating arch twins"
for arch in "${platforms[@]}"; do
  prefix=$(arch_prefix "${arch}")
  src="${IMAGE_PATH}:${prefix}-${PRIMARY_TAG}"
  twins=()
  for t in "${tags[@]}"; do
    [[ "${t}" == "${PRIMARY_TAG}" ]] && continue
    twins+=(-t "${IMAGE_PATH}:${prefix}-${t}")
  done
  if (( ${#twins[@]} > 0 )); then
    retry_ghcr docker buildx imagetools create "${twins[@]}" "${src}"
  fi
done
endgroup

index_digest=$(retry_ghcr docker buildx imagetools inspect "${IMAGE_PATH}:${PRIMARY_TAG}" --format '{{json .Manifest.Digest}}' | tr -d '"')
[[ "${index_digest}" =~ ^sha256:[0-9a-f]{64}$ ]] || die "unexpected index digest ${index_digest}"

published=()
for t in "${tags[@]}"; do
  published+=("${IMAGE_PATH}:${t}")
  for arch in "${platforms[@]}"; do published+=("${IMAGE_PATH}:$(arch_prefix "${arch}")-${t}"); done
done
set_output index_digest "${index_digest}"
set_output published "$(printf '%s\n' "${published[@]}" | jq -R . | jq -sc .)"
{
  echo "### Published"
  echo
  echo "Index digest \`${index_digest}\`"
  echo
  for p in "${published[@]}"; do echo "- \`${p}\`"; done
} >> "${GITHUB_STEP_SUMMARY:-/dev/stdout}"
