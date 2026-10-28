#!/usr/bin/env python3
"""
Retry network calls that failed for a reason that may pass: the Python twin of lib.sh's ``retry``.

    from netretry import retry, run
    data = retry(lambda: fetch(url), what=f"GET {url}")          # an exception that is_transient() accepts retries
    proc = run(["gh", "api", path])                               # a subprocess whose stderr reads as transient retries

A failure is transient when it is the network's or the far end's, never the request's: connection reset, refused,
closed or aborted, DNS failures, timeouts, TLS handshake errors, a truncated response, HTTP 500/502/503/504 and
HTTP 429 or a rate limit. A 404, 401, 403 (other than a rate limit) or any other client error returns at once, as
does every other exception. ``TRANSIENT_TEXT`` is the same list as lib.sh's, for the text a command prints; change
both together.

Attempts and delays follow lib.sh: 5 attempts, waits of about 10, 20, 40 and 80 s (2.5 min in all, each capped at
120 s), with jitter between half and one and a half times the delay. RETRY_MAX_ATTEMPTS, RETRY_BASE_DELAY and
RETRY_MAX_DELAY override them; tests pass ``sleep`` or set the base delay to 0. Standard library only.

``scripts/resolve_version.py`` keeps a copy of its own (``_with_retries``): the control plane imports it as one file.
"""

from __future__ import annotations

import errno
import http.client
import os
import random
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

T = TypeVar("T")

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
"""HTTP statuses worth another attempt."""

NETWORK_ERRNOS = frozenset(
    {
        errno.ECONNRESET,
        errno.ECONNREFUSED,
        errno.ECONNABORTED,
        errno.ETIMEDOUT,
        errno.EPIPE,
        errno.ENETDOWN,
        errno.ENETUNREACH,
        errno.ENETRESET,
        errno.EHOSTDOWN,
        errno.EHOSTUNREACH,
    }
)

_THROTTLED = (
    r"retry-after:|toomanyrequests|too many requests|rate limit"
    r"|http(/[0-9.]+)? (error )?429|(status|status code|returned error|error)[: ]+429"
)
_NETWORK = (
    r"connection (reset|refused|closed|aborted|timed out|broken)|reset by peer|broken pipe"
    r"|failure when receiving data|failed sending data|recv failure|send failure|remote ?disconnected"
    r"|incomplete ?read|max retries exceeded|empty reply from server|the remote end hung up|early eof"
    r"|unexpected eof|unexpected disconnect|stream error|goaway"
    r"|could not resolve (host|proxy)|temporary failure in name resolution|name or service not known"
    r"|no such host|network is unreachable|no route to host|host is unreachable"
    r"|timed out|i/o timeout|tls handshake|deadline exceeded|ssl_connect|ssl connect error"
    r"|ssl_error_syscall|gnutls_handshake|gnutls recv error"
    r"|http(/[0-9.]+)? (error )?50[0234]\b|(status|status code|returned error|error)[: ]+50[0234]\b"
    r"|internal server error|bad gateway|service unavailable|gateway time-?out"
)
THROTTLED_TEXT = re.compile(_THROTTLED, re.IGNORECASE | re.MULTILINE)
TRANSIENT_TEXT = re.compile(_NETWORK, re.IGNORECASE | re.MULTILINE)
FATAL_TEXT = re.compile(r"did not complete successfully", re.IGNORECASE)
"""A Dockerfile step's own failure: only throttling is retried then, as in lib.sh."""


def is_transient_text(text: str) -> bool:
    """Does a failed command's output (stderr) say the failure may pass?"""
    if THROTTLED_TEXT.search(text):
        return True
    if FATAL_TEXT.search(text):
        return False
    return bool(TRANSIENT_TEXT.search(text))


def is_transient_status(status: int) -> bool:
    return status in RETRY_STATUS


def is_transient(exc: BaseException) -> bool:
    """Is this exception from a urllib call (or a socket) a failure that may pass?"""
    if isinstance(exc, urllib.error.HTTPError):
        # GitHub answers a secondary rate limit with 403 and Retry-After, an exhausted quota with 403 and no requests left.
        headers = exc.headers if exc.headers is not None else {}
        limited = exc.code == 403 and (headers.get("Retry-After") or headers.get("X-RateLimit-Remaining") == "0")
        return is_transient_status(exc.code) or bool(limited)
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, BaseException):
            return is_transient(reason)
        return is_transient_text(str(reason))
    if isinstance(exc, ssl.SSLCertVerificationError):
        return False
    if isinstance(exc, (TimeoutError, socket.timeout, socket.gaierror, ConnectionError, ssl.SSLError)):
        return True
    if isinstance(exc, (http.client.IncompleteRead, http.client.RemoteDisconnected, http.client.BadStatusLine)):
        return True
    if isinstance(exc, OSError):
        return exc.errno in NETWORK_ERRNOS or is_transient_text(str(exc))
    return False


def _setting(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def delays(attempts: int | None = None, base: float | None = None, cap: float | None = None) -> list[float]:
    """The waits between attempts, without jitter: base * 2^(n-1) capped at cap, one fewer than the attempts."""
    attempts = int(_setting("RETRY_MAX_ATTEMPTS", 5)) if attempts is None else attempts
    base = _setting("RETRY_BASE_DELAY", 10) if base is None else base
    cap = _setting("RETRY_MAX_DELAY", 120) if cap is None else cap
    return [min(cap, base * 2**n) for n in range(max(1, attempts) - 1)]


def _jitter(delay: float) -> float:
    return delay / 2 + random.uniform(0, delay)  # noqa: S311 - spreading retries, not security


def _say(message: str) -> None:
    print(f"retry: {message}", file=sys.stderr)


def retry(
    fn: Callable[[], T],
    *,
    what: str = "",
    classify: Callable[[BaseException], bool] = is_transient,
    attempts: int | None = None,
    sleep: Callable[[float], Any] = time.sleep,
) -> T:
    """Call fn until it returns; an exception classify() rejects, or the last attempt's, is raised."""
    waits = delays(attempts)
    for n, wait in enumerate([*waits, None], start=1):
        try:
            return fn()
        except Exception as exc:
            if wait is None or not classify(exc):
                raise
            pause = _jitter(wait)
            _say(f"attempt {n}/{len(waits) + 1}{' of ' + what if what else ''} failed ({exc}); waiting {pause:.0f}s...")
            sleep(pause)
    raise AssertionError("unreachable")


def run(
    cmd: Sequence[str],
    *,
    input: str | None = None,  # noqa: A002 - subprocess.run's name
    attempts: int | None = None,
    sleep: Callable[[float], Any] = time.sleep,
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """subprocess.run(cmd, capture_output, text) again while it fails with transient stderr; the last result returns
    whatever it is, so callers keep their own error handling. The stderr of a failed attempt is printed."""
    waits = delays(attempts)
    for n, wait in enumerate([*waits, None], start=1):
        proc = subprocess.run(list(cmd), input=input, capture_output=True, text=True, check=False, **kwargs)
        if proc.returncode == 0 or wait is None or not is_transient_text(proc.stderr or ""):
            return proc
        pause = _jitter(wait)
        sys.stderr.write(proc.stderr if proc.stderr.endswith("\n") else proc.stderr + "\n")
        _say(f"attempt {n}/{len(waits) + 1} of {' '.join(cmd[:3])} failed (exit {proc.returncode}); waiting {pause:.0f}s...")
        sleep(pause)
    raise AssertionError("unreachable")
