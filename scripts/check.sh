#!/usr/bin/env bash
# Everything CI runs. Needs the dev venv: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ -x .venv/bin/python ]]; then
  PATH="${PWD}/.venv/bin:${PATH}"
fi
export PATH

echo "==> shellcheck"
mapfile -t scripts < <(find scripts .github/actions fixtures -type f -name '*.sh' | sort)
mapfile -t fixture_scripts < <(find fixtures -type f -name run -o -type f -name finish | sort)
shellcheck -x "${scripts[@]}"
shellcheck -s bash "${fixture_scripts[@]}"

echo "==> actionlint"
if command -v actionlint >/dev/null 2>&1; then
  actionlint .github/workflows/*.yml
else
  echo "actionlint not installed; the YAML parse test covers the workflows"
fi

echo "==> pytest"
python3 -m pytest -q "$@"

echo "==> contracts"
python3 -m contracts image fixtures/example-image/image.yml
echo "all checks passed"
