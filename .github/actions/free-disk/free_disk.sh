#!/usr/bin/env bash
# Remove toolchains an image build never needs. Everything here is best effort.
set -euo pipefail

before=$(df --output=avail -BG / | tail -1 | tr -dc '0-9')
echo "::group::Freeing disk space (available before: ${before}G)"
sudo rm -rf /usr/share/dotnet /usr/local/lib/android /opt/ghc /opt/hostedtoolcache/CodeQL \
  /usr/local/share/boost /usr/share/swift /usr/local/.ghcup /opt/microsoft /usr/lib/jvm 2>/dev/null || true
sudo docker image prune -af >/dev/null 2>&1 || true
sudo docker builder prune -af >/dev/null 2>&1 || true
sudo apt-get clean >/dev/null 2>&1 || true
after=$(df --output=avail -BG / | tail -1 | tr -dc '0-9')
echo "available after: ${after}G"
echo "::endgroup::"
