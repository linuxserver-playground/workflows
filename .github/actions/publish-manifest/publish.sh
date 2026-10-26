#!/usr/bin/env bash
# docker buildx imagetools create: one index carrying every unprefixed tag, then each arch twin.
#
# Sources are the per-arch staging references the arch jobs pushed (SOURCES, a JSON object
# arch -> ref under ghcr.io/<owner>/build/<name>). imagetools create can tag a manifest that lives in
# another repository of the same registry, so the released tags only come into existence here, after
# every build and test has passed. Without SOURCES the legacy layout is assumed: the arch jobs pushed
# <image_path>:<arch prefix>-<primary tag> themselves.
set -euo pipefail
# shellcheck source=../../../scripts/lib.sh
source "${GITHUB_ACTION_PATH}/../../../scripts/lib.sh"

: "${IMAGE_PATH:?}" "${PRIMARY_TAG:?}" "${TAGS:?}" "${PLATFORMS:?}"
SOURCES="${SOURCES:-}"
DESCRIPTION="${DESCRIPTION:-}"
SBOM_DIR="${SBOM_DIR:-}"

mapfile -t tags < <(jq -r '.[]' <<< "${TAGS}")
mapfile -t platforms < <(jq -r '.[]' <<< "${PLATFORMS}")
(( ${#tags[@]} > 0 )) || die "no tags to publish"
(( ${#platforms[@]} > 0 )) || die "no platforms"

# source_for <arch>: the manifest the released tags of that platform are created from.
declare -A source_for=()
staged=false
if [[ -n "${SOURCES}" && "${SOURCES}" != "{}" && "${SOURCES}" != "null" ]]; then
  staged=true
  for arch in "${platforms[@]}"; do
    ref=$(jq -r --arg a "${arch}" '.[$a] // ""' <<< "${SOURCES}")
    [[ -n "${ref}" ]] || die "no staging reference for ${arch} in SOURCES"
    source_for["${arch}"]="${ref}"
  done
else
  for arch in "${platforms[@]}"; do
    source_for["${arch}"]="${IMAGE_PATH}:$(arch_prefix "${arch}")-${PRIMARY_TAG}"
  done
fi

sources=()
for arch in "${platforms[@]}"; do sources+=("${source_for[${arch}]}"); done

# The index annotation gives the GHCR package page its description; the image labels are untouched.
annotation=()
if [[ -n "${DESCRIPTION}" ]]; then
  annotation=(--annotation "index:org.opencontainers.image.description=${DESCRIPTION}")
fi

# The packages-only SBOM from each arch's ci run joins the provenance in that platform's attestation manifest
# on the source, so the released index created below carries it (BuildKit's own --sbom scan is off).
if [[ -n "${SBOM_DIR}" ]]; then
  group "Attaching SBOMs"
  for arch in "${platforms[@]}"; do
    sbom="${SBOM_DIR}/sbom.${arch}.spdx.json"
    if [[ ! -f "${sbom}" ]]; then
      warn "no ${sbom}; ${arch} is published without an SBOM attestation"
      continue
    fi
    python3 "${GITHUB_ACTION_PATH}/../../../scripts/attach_sbom.py" --ref "${source_for[${arch}]}" --arch "${arch}" --sbom "${sbom}" \
      || die "could not attach the SBOM for ${arch}"
  done
  endgroup
fi

group "Creating index ${IMAGE_PATH}:${PRIMARY_TAG} with ${#tags[@]} tags from ${sources[*]}"
targets=()
for t in "${tags[@]}"; do targets+=(-t "${IMAGE_PATH}:${t}"); done
retry_ghcr docker buildx imagetools create "${annotation[@]}" "${targets[@]}" "${sources[@]}"
endgroup

group "Creating arch twins"
for arch in "${platforms[@]}"; do
  prefix=$(arch_prefix "${arch}")
  src="${source_for[${arch}]}"
  twins=()
  for t in "${tags[@]}"; do
    # In the legacy layout the arch primary already exists: it is the source itself.
    [[ "${staged}" == "false" && "${t}" == "${PRIMARY_TAG}" ]] && continue
    twins+=(-t "${IMAGE_PATH}:${prefix}-${t}")
  done
  if (( ${#twins[@]} > 0 )); then
    retry_ghcr docker buildx imagetools create "${annotation[@]}" "${twins[@]}" "${src}"
  fi
done
endgroup

index_digest=$(retry_ghcr docker buildx imagetools inspect "${IMAGE_PATH}:${PRIMARY_TAG}" --format '{{json .Manifest.Digest}}' | tr -d '"')
[[ "${index_digest}" =~ ^sha256:[0-9a-f]{64}$ ]] || die "unexpected index digest ${index_digest}"
if [[ -n "${SBOM_DIR}" ]]; then
  sbom_platforms=$(docker buildx imagetools inspect "${IMAGE_PATH}:${PRIMARY_TAG}" --format '{{json .SBOM}}' 2>/dev/null \
    | jq -r 'if has("SPDX") then {"single platform": .} else . end | to_entries[] | select(.value.SPDX != null)
             | "\(.key) (\(.value.SPDX.packages | length) packages)"' 2>/dev/null | paste -sd, - || true)
  notice "SBOM attestations on ${IMAGE_PATH}:${PRIMARY_TAG}: ${sbom_platforms:-none}"
fi

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
  if [[ "${staged}" == "true" ]]; then
    echo
    echo "From staging references (deleted by cleanup-staging after 7 days):"
    for arch in "${platforms[@]}"; do echo "- \`${arch}\`: \`${source_for[${arch}]}\`"; done
  fi
  echo
  for p in "${published[@]}"; do echo "- \`${p}\`"; done
} >> "${LSIO_SUMMARY:-${GITHUB_STEP_SUMMARY:-/dev/stdout}}"
