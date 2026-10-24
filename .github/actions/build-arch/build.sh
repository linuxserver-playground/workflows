#!/usr/bin/env bash
# Build one architecture. Expects the buildx builder to be set up and, when pushing, docker to be
# logged in to ghcr.io. Writes ${OUT_DIR}/arch.${ARCH}.json and ${OUT_DIR}/dockerfile.${ARCH}.
set -euo pipefail
# shellcheck source=../../../scripts/lib.sh
source "${GITHUB_ACTION_PATH}/../../../scripts/lib.sh"

: "${ARCH:?}" "${DOCKERFILE:?}" "${IMAGE_PATH:?}" "${ARCH_TAG:?}" "${NAME:?}" "${TITLE:?}" "${VERSION_LABEL:?}"
: "${MODE:?}" "${BRANCH:?}" "${SHA:?}" "${UPSTREAM:?}" "${REPOSITORY:?}" "${BUILD_DATE:?}" "${OUT_DIR:?}"
PUSH="${PUSH:-true}"
FIRST_PARTY="${FIRST_PARTY:-true}"
BUILD_ARGS="${BUILD_ARGS:-{\}}"
DESCRIPTION="${DESCRIPTION:-${NAME} image by linuxserver.io}"

mkdir -p "${OUT_DIR}"
work="${RUNNER_TEMP:-/tmp}/lsio-build-${ARCH}"
mkdir -p "${work}"
platform=$(docker_platform "${ARCH}")
repo_url="https://github.com/${REPOSITORY}"

# ---- Dockerfile with ENV LSIO_FIRST_PARTY=true after FROM (CONVENTIONS §5) ----
[[ -f "${DOCKERFILE}" ]] || die "Dockerfile ${DOCKERFILE} not found"
built_dockerfile="${work}/Dockerfile"
cp "${DOCKERFILE}" "${built_dockerfile}"
if [[ "${FIRST_PARTY}" == "true" ]]; then
  sed -r -i 's|(^FROM .*)|\1\n\nENV LSIO_FIRST_PARTY=true|g' "${built_dockerfile}"
fi
cp "${built_dockerfile}" "${OUT_DIR}/dockerfile.${ARCH}"

# ---- Base image with digest ----
base_image=$(awk 'toupper($1)=="FROM" { for (i=2;i<=NF;i++) if ($i !~ /^--/) { print $i; exit } }' "${built_dockerfile}" | head -1)
base_digest=""
base_ref="${base_image}"
if [[ -n "${base_image}" && "${base_image}" != "scratch" ]]; then
  if base_digest=$(retry_ghcr docker buildx imagetools inspect "${base_image}" --format '{{json .Manifest.Digest}}' 2>/dev/null | tr -d '"'); then
    [[ -n "${base_digest}" ]] && base_ref="${base_image}@${base_digest}"
  else
    warn "could not resolve a digest for base image ${base_image}"
    base_digest=""
  fi
fi

# ---- Labels (CONVENTIONS §5) ----
labels=(
  "org.opencontainers.image.created=${BUILD_DATE}"
  "org.opencontainers.image.authors=linuxserver.io"
  "org.opencontainers.image.url=${repo_url}/packages"
  "org.opencontainers.image.documentation=https://docs.linuxserver.io/images/docker-${NAME}"
  "org.opencontainers.image.source=${repo_url}"
  "org.opencontainers.image.version=${VERSION_LABEL}"
  "org.opencontainers.image.revision=${SHA}"
  "org.opencontainers.image.vendor=linuxserver.io"
  "org.opencontainers.image.licenses=GPL-3.0-only"
  "org.opencontainers.image.title=${TITLE}"
  "org.opencontainers.image.description=${DESCRIPTION}"
  "io.linuxserver.image.mode=${MODE}"
  "io.linuxserver.image.branch=${BRANCH}"
  "io.linuxserver.image.upstream=${UPSTREAM}"
  "io.linuxserver.image.base=${base_ref}"
  "build_version=Linuxserver.io version:- ${VERSION_LABEL} Build-date:- ${BUILD_DATE}"
)
args=()
for l in "${labels[@]}"; do args+=(--label "${l}"); done

# ---- Build args ----
if [[ -n "${VERSION_ARG}" ]]; then args+=(--build-arg "${VERSION_ARG}=${UPSTREAM}"); fi
args+=(--build-arg "VERSION=${VERSION_LABEL}" --build-arg "BUILD_DATE=${BUILD_DATE}")
while IFS= read -r kv; do
  [[ -n "${kv}" ]] && args+=(--build-arg "${kv}")
done < <(jq -r 'to_entries[] | "\(.key)=\(.value)"' <<< "${BUILD_ARGS}")

remote_ref="${IMAGE_PATH}:${ARCH_TAG}"
local_ref="lsio-build/${NAME}:${ARCH_TAG}"
meta="${work}/meta.json"

group "docker buildx build ${ARCH} (push=${PUSH})"
start=${SECONDS}
if [[ "${PUSH}" == "true" ]]; then
  retry_ghcr docker buildx build --no-cache --pull --platform "${platform}" -f "${built_dockerfile}" \
    "${args[@]}" --provenance=mode=max --sbom=true --push -t "${remote_ref}" --metadata-file "${meta}" .
  # Test against exactly what was pushed.
  retry_ghcr docker pull --platform "${platform}" "${remote_ref}"
  image_ref="${remote_ref}"
else
  docker buildx build --no-cache --pull --platform "${platform}" -f "${built_dockerfile}" \
    "${args[@]}" --load -t "${local_ref}" --metadata-file "${meta}" .
  image_ref="${local_ref}"
fi
build_seconds=$(( SECONDS - start ))
endgroup

# ---- Digest and sizes ----
uncompressed_size=$(docker image inspect --format '{{.Size}}' "${image_ref}")
layer_count=$(docker image inspect --format '{{len .RootFS.Layers}}' "${image_ref}")
digest=""
size=""
if [[ "${PUSH}" == "true" ]]; then
  raw=$(retry_ghcr docker buildx imagetools inspect "${remote_ref}" --raw)
  if jq -e '.manifests' <<< "${raw}" >/dev/null 2>&1; then
    goarch="${ARCH}"
    digest=$(jq -r --arg a "${goarch}" '[.manifests[] | select(.platform.architecture == $a and .platform.os == "linux")][0].digest // ""' <<< "${raw}")
    [[ -n "${digest}" ]] || die "no ${ARCH} manifest in the pushed index for ${remote_ref}"
    manifest_json=$(retry_ghcr docker buildx imagetools inspect "${IMAGE_PATH}@${digest}" --raw)
  else
    digest=$(jq -r '.["containerimage.digest"] // ""' "${meta}")
    manifest_json="${raw}"
  fi
  size=$(jq -r '[.layers[].size] | add // 0' <<< "${manifest_json}")
  layer_count=$(jq -r '.layers | length' <<< "${manifest_json}")
else
  digest=$(jq -r '.["containerimage.digest"] // ""' "${meta}")
  if [[ ! "${digest}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    digest=$(docker image inspect --format '{{.Id}}' "${image_ref}")
  fi
  size="${uncompressed_size}"
  group "docker save ${image_ref}"
  docker save "${image_ref}" | gzip -1 > "${OUT_DIR}/image.${ARCH}.tar.gz"
  endgroup
fi

labels_json=$(docker image inspect --format '{{json .Config.Labels}}' "${image_ref}")
jq -n \
  --arg arch "${ARCH}" --arg digest "${digest}" --argjson size "${size:-0}" \
  --argjson uncompressed_size "${uncompressed_size}" --argjson layers "${layer_count}" \
  --arg runner "${RUNNER_LABEL:-unknown}" --argjson build_seconds "${build_seconds}" \
  --argjson pushed "$([[ "${PUSH}" == "true" ]] && echo true || echo false)" \
  --arg image_ref "${image_ref}" --arg tag "${ARCH_TAG}" \
  --arg base_image "${base_image}" --arg base_digest "${base_digest}" \
  --argjson labels "${labels_json}" --rawfile dockerfile "${built_dockerfile}" \
  '{arch:$arch, digest:$digest, size:$size, uncompressed_size:$uncompressed_size, layers:$layers, runner:$runner,
    build_seconds:$build_seconds, pushed:$pushed, image_ref:$image_ref, tag:$tag, base_image:$base_image,
    base_digest:$base_digest, labels:$labels, dockerfile:$dockerfile}' > "${OUT_DIR}/arch.${ARCH}.json"

set_output digest "${digest}"
set_output size "${size}"
set_output uncompressed_size "${uncompressed_size}"
set_output layers "${layer_count}"
set_output image_ref "${image_ref}"
set_output build_seconds "${build_seconds}"
set_output base_digest "${base_digest}"
summary "- \`${ARCH}\`: built \`${image_ref}\` in ${build_seconds}s, digest \`${digest}\`, base \`${base_ref}\`"
