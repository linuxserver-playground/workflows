#!/usr/bin/env bash
# Publish an approved fork pull request's image from the Build run's image-<arch> artifacts.
#
#   IMAGE_PATH=ghcr.io/<org>/untrusted-pr/<name> PRIMARY_TAG=pr-<n>-<sha7> IMAGES_DIR=images SBOM_DIR=bundle \
#     publish_pr_image.sh
#
# Runs in the trusted PR report, so nothing from the image is executed: `docker load` only unpacks it. Each
# arch must load as exactly linux/<arch>; it is pushed as <arch prefix>-<primary tag>, gets the build's SBOM
# attached like a same-repository PR build, and the primary tag becomes the multi-arch index of them.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

: "${IMAGE_PATH:?}" "${PRIMARY_TAG:?}" "${IMAGES_DIR:?}"
SBOM_DIR="${SBOM_DIR:-}"
[[ "${IMAGE_PATH}" =~ ^ghcr\.io/[a-z0-9-]+/untrusted-pr/[a-z0-9._-]+$ ]] || die "refusing to publish outside untrusted-pr: ${IMAGE_PATH}"
[[ "${PRIMARY_TAG}" =~ ^pr-[0-9]+-[0-9a-f]{7}$ ]] || die "unexpected tag ${PRIMARY_TAG}"

sources=()
for arch in amd64 arm64; do
  archive=$(find "${IMAGES_DIR}" -maxdepth 2 -type f -name "image.${arch}.tar.gz" | head -1)
  [[ -n "${archive}" ]] || continue
  group "Loading the ${arch} image"
  loaded=$(docker load -i "${archive}" | sed -n 's/^Loaded image: //p' | tail -1)
  [[ -n "${loaded}" ]] || die "image.${arch}.tar.gz loaded no tagged image"
  platform=$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "${loaded}")
  [[ "${platform}" == "linux/${arch}" ]] || die "image.${arch}.tar.gz is ${platform}, not linux/${arch}"
  target="${IMAGE_PATH}:$(arch_prefix "${arch}")-${PRIMARY_TAG}"
  docker tag "${loaded}" "${target}"
  retry_ghcr docker push "${target}"
  endgroup
  sbom="${SBOM_DIR:+${SBOM_DIR}/sbom.${arch}.spdx.json}"
  if [[ -n "${sbom}" && -f "${sbom}" ]]; then
    python3 "$(dirname "${BASH_SOURCE[0]}")/attach_sbom.py" --ref "${target}" --arch "${arch}" --sbom "${sbom}" \
      || warn "could not attach the ${arch} SBOM"
  fi
  sources+=("${target}")
done
(( ${#sources[@]} > 0 )) || die "the build left no image-<arch> artifacts to publish"

group "Creating ${IMAGE_PATH}:${PRIMARY_TAG}"
retry_ghcr docker buildx imagetools create -t "${IMAGE_PATH}:${PRIMARY_TAG}" "${sources[@]}"
endgroup
notice "published ${IMAGE_PATH}:${PRIMARY_TAG} from ${#sources[@]} architecture(s)"
set_output published true
