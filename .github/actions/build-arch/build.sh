#!/usr/bin/env bash
# Build one architecture. Expects the buildx builder to be set up and, when pushing, docker to be
# logged in to ghcr.io. Writes ${OUT_DIR}/arch.${ARCH}.json and ${OUT_DIR}/dockerfile.${ARCH}.
#
# When STAGING_REF is set the image is pushed there (ghcr.io/<owner>/build/<name>:<arch>-<sha7>-<run_id>)
# instead of <image_path>:<arch_tag>, so nothing a consumer could pull is touched before the whole run
# has succeeded; publish-manifest creates the released tags from the staging references. The staging
# package is marked "Intermediate build artefact, not for use" through an *index annotation*, not an
# image label: imagetools create copies the image config verbatim into the released repository, so a
# label would follow it, while index annotations stay on the staging index.
set -euo pipefail
# shellcheck source=../../../scripts/lib.sh
source "${GITHUB_ACTION_PATH}/../../../scripts/lib.sh"

: "${ARCH:?}" "${DOCKERFILE:?}" "${IMAGE_PATH:?}" "${ARCH_TAG:?}" "${NAME:?}" "${TITLE:?}" "${VERSION_LABEL:?}"
: "${MODE:?}" "${BRANCH:?}" "${SHA:?}" "${UPSTREAM:?}" "${REPOSITORY:?}" "${BUILD_DATE:?}" "${OUT_DIR:?}"
PUSH="${PUSH:-true}"
STAGING_REF="${STAGING_REF:-}"
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
# The last FROM is the runtime stage; earlier stages only build or supply files (COPY --from).
base_image=$(awk 'toupper($1)=="FROM" { for (i=2;i<=NF;i++) if ($i !~ /^--/) { ref=$i; break } } END { print ref }' "${built_dockerfile}")
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
# No --sbom: BuildKit's scan records every file and outgrows its 80 MiB attestation limit on desktop images.
# The ci run writes a packages-only SBOM and publish-manifest attaches it in the same attestation layout.
if [[ -n "${VERSION_ARG}" ]]; then args+=(--build-arg "${VERSION_ARG}=${UPSTREAM}"); fi
args+=(--build-arg "VERSION=${VERSION_LABEL}" --build-arg "BUILD_DATE=${BUILD_DATE}")
while IFS= read -r kv; do
  [[ -n "${kv}" ]] && args+=(--build-arg "${kv}")
done < <(jq -r 'to_entries[] | "\(.key)=\(.value)"' <<< "${BUILD_ARGS}")

if [[ -n "${STAGING_REF}" ]]; then
  remote_ref="${STAGING_REF}"
  args+=(--annotation "index:org.opencontainers.image.description=Intermediate build artefact, not for use")
else
  remote_ref="${IMAGE_PATH}:${ARCH_TAG}"
fi
remote_repo="${remote_ref%:*}"
local_ref="lsio-build/${NAME}:${ARCH_TAG}"
meta="${work}/meta.json"

group "docker buildx build ${ARCH} (push=${PUSH}${STAGING_REF:+, staging})"
start=${SECONDS}
if [[ "${PUSH}" == "true" ]]; then
  retry_ghcr docker buildx build --no-cache --pull --platform "${platform}" -f "${built_dockerfile}" \
    "${args[@]}" --provenance=mode=max --sbom=false --push -t "${remote_ref}" --metadata-file "${meta}" .
  # Test against exactly what was pushed. GHCR can answer "manifest unknown" for a few seconds after the
  # push it just accepted, so that is retried here too.
  RETRY_ALSO="manifest unknown" retry_ghcr docker pull --platform "${platform}" "${remote_ref}"
  image_ref="${remote_ref}"
else
  retry_ghcr docker buildx build --no-cache --pull --platform "${platform}" -f "${built_dockerfile}" \
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
    manifest_json=$(retry_ghcr docker buildx imagetools inspect "${remote_repo}@${digest}" --raw)
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
  --arg image_ref "${image_ref}" --arg tag "${ARCH_TAG}" --arg staging_ref "${STAGING_REF}" \
  --arg base_image "${base_image}" --arg base_digest "${base_digest}" \
  --argjson labels "${labels_json}" --rawfile dockerfile "${built_dockerfile}" \
  '{arch:$arch, digest:$digest, size:$size, uncompressed_size:$uncompressed_size, layers:$layers, runner:$runner,
    build_seconds:$build_seconds, pushed:$pushed, image_ref:$image_ref, tag:$tag, staging_ref:$staging_ref,
    base_image:$base_image, base_digest:$base_digest, labels:$labels, dockerfile:$dockerfile}' > "${OUT_DIR}/arch.${ARCH}.json"

set_output digest "${digest}"
set_output size "${size}"
set_output uncompressed_size "${uncompressed_size}"
set_output layers "${layer_count}"
set_output image_ref "${image_ref}"
set_output build_seconds "${build_seconds}"
set_output base_digest "${base_digest}"
set_output staging_ref "${STAGING_REF}"
summary "- \`${ARCH}\`: built \`${image_ref}\`${STAGING_REF:+ (staging)} in ${build_seconds}s, digest \`${digest}\`, base \`${base_ref}\`"
