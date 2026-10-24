#!/usr/bin/env bash
# Static checks of the prepare job: shellcheck over root/ scripts (annotations, non-blocking) and
# the s6 executable-bit check (blocking).
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

root_dir="${1:-root}"
if [[ ! -d "${root_dir}" ]]; then
  notice "no ${root_dir}/ directory; nothing to check"
  exit 0
fi

# ---- shellcheck ----
group "shellcheck ${root_dir}/"
# shellcheck disable=SC2016
mapfile -t scripts < <(find "${root_dir}" -type f -print0 | xargs -0 awk 'FNR==1 { if ($0 ~ /^#!.*(ba|da|a|k)?sh([ \t]|$)/ || $0 ~ /^#!\/usr\/bin\/with-contenv/) print FILENAME; nextfile }')
count=0
findings=0
if command -v shellcheck >/dev/null 2>&1; then
  for f in "${scripts[@]}"; do
    count=$((count + 1))
    # with-contenv scripts are bash; tell shellcheck so.
    while IFS= read -r line; do
      [[ -z "${line}" ]] && continue
      findings=$((findings + 1))
      file=${line%%:*}; rest=${line#*:}; lineno=${rest%%:*}; rest=${rest#*:}; col=${rest%%:*}; msg=${rest#*: }
      echo "::warning file=${file},line=${lineno},col=${col}::shellcheck: ${msg}"
    done < <(shellcheck -s bash -f gcc -S warning "${f}" 2>/dev/null || true)
  done
  echo "shellcheck: ${count} script(s), ${findings} finding(s)"
  summary "shellcheck: ${count} script(s) in \`${root_dir}/\`, ${findings} warning(s)"
else
  warn "shellcheck not installed on this runner; skipping"
fi
endgroup

# ---- s6 executable bits ----
group "s6 service file permissions"
wrong=$(find "${root_dir}" -path '*/s6-overlay/s6-rc.d/*' -type f \( -name run -o -name finish -o -name check \) ! -perm -u=x,g=x,o=x -print)
# Legacy locations still used by some images.
wrong+=$(find "${root_dir}" \( -path '*/cont-init.d/*' -o -path '*/services.d/*' \) -type f ! -perm -u=x,g=x,o=x -print 2>/dev/null || true)
if [[ -n "${wrong// /}" ]]; then
  while IFS= read -r f; do [[ -n "${f}" ]] && echo "::error file=${f}::missing executable bit"; done <<< "${wrong}"
  die "s6 service files are missing the executable bit: ${wrong//$'\n'/ }"
fi
echo "s6 service file permissions look good."
endgroup
