#!/usr/bin/env bash
# Shared helpers for the composite actions. Source it; do not execute it.
#
#   source "${GITHUB_ACTION_PATH}/../../../scripts/lib.sh"
#
# Everything here is plain bash so it runs on any hosted runner.

# Print a GitHub Actions log group header.
group() { echo "::group::$*"; }
endgroup() { echo "::endgroup::"; }

# Annotations.
warn() { echo "::warning::$*"; }
error() { echo "::error::$*"; }
notice() { echo "::notice::$*"; }

# die <message>: error annotation and exit 1.
die() {
  error "$*"
  exit 1
}

# set_output <name> <value>: write a step output, multi-line safe.
set_output() {
  local name="$1" value="$2" delim
  delim="lsio_$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  {
    echo "${name}<<${delim}"
    echo "${value}"
    echo "${delim}"
  } >> "${GITHUB_OUTPUT:-/dev/stdout}"
}

# set_env <name> <value>: export into following steps.
set_env() {
  local name="$1" value="$2" delim
  delim="lsio_$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  {
    echo "${name}<<${delim}"
    echo "${value}"
    echo "${delim}"
  } >> "${GITHUB_ENV:-/dev/stdout}"
}

# summary <markdown>: append to LSIO_SUMMARY when the job collects its notes there (image builds: the Publish
# job's last step puts them under its report, the other jobs send them to the log), else to the step summary.
summary() { echo "$*" >> "${LSIO_SUMMARY:-${GITHUB_STEP_SUMMARY:-/dev/stdout}}"; }

# rfc3339_now: build timestamp with offset, as the labels want it.
rfc3339_now() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

# ---- Retries ----
#
# retry and retry_ghcr run a command and run it again only when it failed for a reason that may pass: the network
# or the service on the other end, never the request itself. The failure is classified from the command's stderr
# (teed, so the caller still sees it live) and, for curl, its exit code. scripts/netretry.py is the Python twin
# of these lists; change both together.
#
# Throttling, always retried. Only HTTP wording counts for status codes: a bare "429" (or "503") also matches the
# digests BuildKit prints, which once retried a deterministic build failure seven times.
_RETRY_THROTTLED='retry-after:|toomanyrequests|too many requests|rate limit'
_RETRY_THROTTLED+='|http(/[0-9.]+)? (error )?429|(status|status code|returned error|error)[: ]+429'
# Network and server failures, retried unless the failure is a build step's own (_RETRY_FATAL).
_RETRY_NETWORK='connection (reset|refused|closed|aborted|timed out|broken)|reset by peer|broken pipe'
_RETRY_NETWORK+='|failure when receiving data|failed sending data|recv failure|send failure|remote ?disconnected'
_RETRY_NETWORK+='|incomplete ?read|max retries exceeded|empty reply from server|the remote end hung up|early eof'
_RETRY_NETWORK+='|unexpected eof|unexpected disconnect|stream error|goaway'
_RETRY_NETWORK+='|could not resolve (host|proxy)|temporary failure in name resolution|name or service not known'
_RETRY_NETWORK+='|no such host|network is unreachable|no route to host|host is unreachable'
_RETRY_NETWORK+='|timed out|i/o timeout|tls handshake|deadline exceeded|ssl_connect|ssl connect error'
_RETRY_NETWORK+='|ssl_error_syscall|gnutls_handshake|gnutls recv error'
_RETRY_NETWORK+='|http(/[0-9.]+)? (error )?50[0234]\b|(status|status code|returned error|error)[: ]+50[0234]\b'
_RETRY_NETWORK+='|internal server error|bad gateway|service unavailable|gateway time-?out'
# A Dockerfile step that failed is the build's own failure, whatever its output says (a RUN that cannot resolve a
# dead mirror would otherwise be rebuilt every attempt): only throttling is retried then.
_RETRY_FATAL='did not complete successfully'
# curl exit codes for network failures: 5/6 proxy/host not resolved, 7 connect failed, 16 HTTP/2 framing,
# 18 partial file, 28 timeout, 35 TLS handshake, 52 empty reply, 55 send failed, 56 receive failed, 92 HTTP/2
# stream. Read only when the command is curl; other tools use these numbers for other things. 22 (-f on an HTTP
# error) is classified by the text curl -S prints with it, so a 404 is not retried and a 503 is.
_RETRY_CURL_EXITS=' 5 6 7 16 18 28 35 52 55 56 92 '

# _retry_transient <exit code> <stderr file> <always ERE> <command>: is this failure worth another attempt?
_retry_transient() {
  local code="$1" errfile="$2" always="$3" cmd="${4##*/}"
  [[ -n "${always}" ]] && grep -q -i -E -e "${always}" "${errfile}" && return 0
  grep -q -i -E -e "${_RETRY_FATAL}" "${errfile}" && return 1
  grep -q -i -E -e "${_RETRY_NETWORK}" "${errfile}" && return 0
  [[ "${cmd}" == "curl" && "${_RETRY_CURL_EXITS}" == *" ${code} "* ]] && return 0
  return 1
}

# _retry_loop <max attempts> <base delay> <max delay> <always ERE> <command...>: the loop behind both helpers.
# Delay before attempt n+1: base * 2^(n-1), capped at max, with jitter between half and one and a half times it so
# parallel builds spread out. A failure that is not transient is returned at once with the command's exit code.
_retry_loop() {
  local max_attempts="$1" base_delay="$2" max_delay="$3" always="$4"
  shift 4
  local attempt=1 errfile exit_code delay pause tee_pid
  errfile=$(mktemp)
  while true; do
    # Run inside an if so a caller's set -e cannot abort before we get to retry. stderr is teed into errfile so the
    # classification can read it while the caller still sees it live.
    if "$@" 2> >(tee "${errfile}" >&2); then
      exit_code=0
    else
      exit_code=$?
    fi
    # Make sure tee has finished writing before we read the file. Under set -u, $! is unset when bash did not
    # record the process substitution's pid, so guard it.
    tee_pid="${!:-}"
    if [[ -n "${tee_pid}" ]]; then
      wait "${tee_pid}" 2>/dev/null || true
    fi
    if (( exit_code == 0 )); then
      rm -f "${errfile}"
      return 0
    elif ! _retry_transient "${exit_code}" "${errfile}" "${always}" "$1"; then
      rm -f "${errfile}"
      return "${exit_code}"
    elif (( attempt >= max_attempts )); then
      rm -f "${errfile}"
      echo "retry: $* failed after ${attempt} attempts (exit ${exit_code})" >&2
      return "${exit_code}"
    fi
    delay=$(( base_delay * 2 ** (attempt - 1) ))
    (( delay > max_delay )) && delay="${max_delay}"
    pause=$(( delay / 2 + RANDOM % (delay + 1) ))
    echo "retry: attempt ${attempt}/${max_attempts} of $1 failed (exit ${exit_code}); waiting ${pause}s..." >&2
    sleep "${pause}"
    ((attempt++))
  done
}

# retry <command...>: run a command that goes over the network (gh, git, curl, pip), retrying network failures,
# HTTP 5xx and throttling (above). 5 attempts, waits of about 10, 20, 40 and 80 s (2.5 min in all, capped at 120 s
# each). RETRY_MAX_ATTEMPTS, RETRY_BASE_DELAY and RETRY_MAX_DELAY override them (tests set the delay to 0);
# RETRY_ALSO adds one more ERE to retry on. Not found, unauthorised, rejected and invalid requests return at once.
retry() {
  _retry_loop "${RETRY_MAX_ATTEMPTS:-5}" "${RETRY_BASE_DELAY:-10}" "${RETRY_MAX_DELAY:-120}" \
    "${_RETRY_THROTTLED}${RETRY_ALSO:+|${RETRY_ALSO}}" "$@"
}

# retry_ghcr <command...>: retry for registry-touching commands (docker push and pull, buildx build and imagetools),
# modelled on the Jenkinsfile's function of the same name: what retry retries, plus GHCR's 403s, over 7 attempts
# capped at 90 s each. A 403 "denied" is retried because when two runs of one repo create a new GHCR package at
# the same moment, the loser's manifest PUT is refused for a few seconds while GHCR grants the repository access.
# RETRY_ALSO adds one more pattern ("manifest unknown" right after a push).
retry_ghcr() {
  _retry_loop "${RETRY_MAX_ATTEMPTS:-7}" "${RETRY_BASE_DELAY:-10}" "${RETRY_MAX_DELAY:-90}" \
    "${_RETRY_THROTTLED}|403 Forbidden|^denied\$${RETRY_ALSO:+|${RETRY_ALSO}}" "$@"
}

# sanitize_tag <string>: make a string legal as a docker tag component.
# Branch names may carry '/', which docker tags cannot.
sanitize_tag() {
  local s="$1"
  s="${s//[^A-Za-z0-9_.-]/-}"
  s="${s#[.-]}"
  printf '%s' "${s:0:120}"
}

# arch_prefix <arch>: the tag prefix for an architecture (amd64 -> amd64, arm64 -> arm64v8).
arch_prefix() {
  case "$1" in
    amd64) echo "amd64" ;;
    arm64) echo "arm64v8" ;;
    *) die "unknown arch $1" ;;
  esac
}

# docker_platform <arch>: buildx platform string.
docker_platform() {
  case "$1" in
    amd64) echo "linux/amd64" ;;
    arm64) echo "linux/arm64" ;;
    *) die "unknown arch $1" ;;
  esac
}
