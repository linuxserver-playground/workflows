#!/usr/bin/env bash
# Make the builder package (lsio-builder: contracts, builder, builder.derive) importable for this job's python3.
#
#   install_builder.sh [ref] [module...]    ref: branch, tag or sha of linuxserver-playground/builder (main)
#
# Nothing is done when the modules already import (builder and contracts unless others are named): an earlier step
# of the same job installed it. Otherwise the source tarball of the ref is downloaded with one HTTPS GET and pip
# installs that file (its PyPI dependencies and build backend come from PyPI as before); both are retried on
# network failures (lib.sh retry). This replaced `pip install git+https://github.com/...`: a git clone is a long
# transfer neither git nor pip retries, and one "Failure when receiving data from the peer" in it failed a release
# job. The builder image is not used instead: the jobs that need the package do not pull it, and the image is far
# larger than the tarball.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ref="${1:-main}"
shift $(( $# > 0 ? 1 : 0 ))
modules=("$@")
(( ${#modules[@]} > 0 )) || modules=(builder contracts)
imports=$(printf 'import %s; ' "${modules[@]}")

if python3 -c "${imports}" 2>/dev/null; then
  echo "builder is already installed (${modules[*]} import)"
  exit 0
fi

repository="linuxserver-playground/builder"
work=$(mktemp -d)
trap 'rm -rf "${work}"' EXIT
tarball="${work}/lsio-builder.tar.gz"
retry curl -sSfL -o "${tarball}" "https://codeload.github.com/${repository}/tar.gz/${ref}"
retry python3 -m pip install --quiet --disable-pip-version-check --user "${tarball}"
python3 -c "${imports}" || die "builder ${ref} installed but ${modules[*]} do not import"
echo "installed builder ${ref} from ${repository}"
