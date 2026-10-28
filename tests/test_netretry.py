"""netretry (the Python twin of lib.sh's retry), resolve_version's inlined retries and the scripts wired to them.
Nothing touches the network and nothing really sleeps: sleep is injected or the base delay is 0."""

from __future__ import annotations

import email.message
import http.client
import io
import json
import socket
import ssl
import urllib.error
from pathlib import Path

import pytest

import netretry
import notify
import package_versions
import pages
import resolve_version as rv

TRANSIENT = [
    "error: RPC failed; curl 56 GnuTLS recv error (-9): Error decoding the received TLS packet.",
    "curl: (56) Failure when receiving data from the peer",
    "curl: (55) Failed sending data to the peer",
    "curl: (7) Failed to connect to github.com port 443: Connection refused",
    "curl: (6) Could not resolve host: codeload.github.com",
    "curl: (28) Operation timed out after 30001 milliseconds with 0 bytes received",
    "curl: (22) The requested URL returned error: 503",
    "curl: (35) OpenSSL SSL_connect: SSL_ERROR_SYSCALL in connection to github.com:443",
    "Post \"https://api.github.com/graphql\": net/http: TLS handshake timeout",
    "Get \"https://ghcr.io/v2/\": dial tcp: lookup ghcr.io on 127.0.0.53:53: no such host",
    "dial tcp 140.82.112.33:443: i/o timeout",
    "read tcp 10.1.0.4:50422->140.82.114.6:443: read: connection reset by peer",
    "fatal: unable to access 'https://github.com/o/r/': Temporary failure in name resolution",
    "fatal: the remote end hung up unexpectedly",
    "unexpected EOF",
    "HTTP 502: Bad Gateway (https://api.github.com/repos/o/r/releases)",
    "HTTP 500: Internal Server Error",
    "error: RPC failed; HTTP 504 curl 22 The requested URL returned error: 504",
    "received unexpected HTTP status: 503 Service Unavailable",
    "ERROR: HTTP error 503 while getting https://files.pythonhosted.org/x.whl",
    "HTTPSConnectionPool(host='pypi.org', port=443): Max retries exceeded with url: /simple/jinja2/",
    "HTTP 429: Too Many Requests",
    "HTTP 403: API rate limit exceeded for installation ID 1.",
    "toomanyrequests: retry-after: 12s",
]
PERMANENT = [
    "HTTP 404: Not Found (https://api.github.com/repos/o/r/git/ref/tags/x)",
    "release not found",
    "HTTP 401: Bad credentials",
    "HTTP 422: Validation Failed (already_exists)",
    " ! [rejected]        HEAD -> main (fetch first)",
    "error: failed to push some refs to 'origin'",
    "curl: (22) The requested URL returned error: 404",
    "no assets match the file pattern",
    "ERROR: manifest unknown",
    "#12 exporting manifest sha256:4290503a1b2c3d4e5f60718293a4b5c6d7e8f9012345678901234567890abcd 0.0s done",
    "ERROR: Could not find a version that satisfies the requirement nope==9",
]


@pytest.mark.parametrize("text", TRANSIENT)
def test_transient_text(text):
    assert netretry.is_transient_text(text)


@pytest.mark.parametrize("text", PERMANENT)
def test_permanent_text(text):
    assert not netretry.is_transient_text(text)


def test_a_dockerfile_step_failure_is_never_a_network_retry_but_throttling_is():
    fatal = 'ERROR: failed to solve: process "/bin/sh -c curl -fsSL https://gone.example" did not complete successfully: exit code: 6'
    assert not netretry.is_transient_text("curl: (6) Could not resolve host: gone.example\n" + fatal)
    assert netretry.is_transient_text("toomanyrequests: slow down\n" + fatal)


def _http_error(code: int, headers: dict[str, str] | None = None) -> urllib.error.HTTPError:
    msg = email.message.Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError("https://x", code, "x", msg, io.BytesIO(b""))


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_http_error(503), True),
        (_http_error(502), True),
        (_http_error(429), True),
        (_http_error(403, {"Retry-After": "30"}), True),
        (_http_error(403, {"X-RateLimit-Remaining": "0"}), True),
        (_http_error(403), False),
        (_http_error(404), False),
        (_http_error(401), False),
        (_http_error(501), False),
        (urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")), True),
        (urllib.error.URLError(socket.gaierror(-3, "Temporary failure in name resolution")), True),
        (urllib.error.URLError("timed out"), True),
        (urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed")), False),
        (urllib.error.URLError("unknown url type: ftp"), False),
        (TimeoutError("The read operation timed out"), True),
        (ConnectionResetError(104, "Connection reset by peer"), True),
        (http.client.RemoteDisconnected("Remote end closed connection without response"), True),
        (http.client.IncompleteRead(b"x", 10), True),
        (ssl.SSLEOFError(8, "EOF occurred in violation of protocol"), True),
        (OSError(101, "Network is unreachable"), True),
        (FileNotFoundError(2, "No such file"), False),
        (ValueError("x"), False),
        (json.JSONDecodeError("x", "", 0), False),
    ],
)
def test_transient_exceptions(exc, expected):
    assert netretry.is_transient(exc) is expected


def test_delays_default_and_overrides(monkeypatch):
    for name in ("RETRY_MAX_ATTEMPTS", "RETRY_BASE_DELAY", "RETRY_MAX_DELAY"):
        monkeypatch.delenv(name, raising=False)
    assert netretry.delays() == [10, 20, 40, 80]
    monkeypatch.setenv("RETRY_MAX_ATTEMPTS", "7")
    monkeypatch.setenv("RETRY_MAX_DELAY", "30")
    assert netretry.delays() == [10, 20, 30, 30, 30, 30]
    assert netretry.delays(attempts=1) == []


def test_retry_retries_transient_failures_with_jittered_backoff(monkeypatch):
    for name in ("RETRY_MAX_ATTEMPTS", "RETRY_BASE_DELAY", "RETRY_MAX_DELAY"):
        monkeypatch.delenv(name, raising=False)
    calls, sleeps = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer"))
        return "ok"

    assert netretry.retry(flaky, sleep=sleeps.append) == "ok"
    assert len(calls) == 3 and len(sleeps) == 2
    assert 5 <= sleeps[0] <= 15 and 10 <= sleeps[1] <= 30


def test_retry_raises_permanent_failures_at_once_and_the_last_one_when_exhausted():
    calls, sleeps = [], []

    def gone():
        calls.append(1)
        raise _http_error(404)

    with pytest.raises(urllib.error.HTTPError):
        netretry.retry(gone, sleep=sleeps.append)
    assert len(calls) == 1 and sleeps == []

    def down():
        calls.append(1)
        raise _http_error(503)

    calls.clear()
    with pytest.raises(urllib.error.HTTPError):
        netretry.retry(down, attempts=4, sleep=sleeps.append)
    assert len(calls) == 4 and len(sleeps) == 3


def _command(tmp_path, stderr: str, code: int, fail_times: int) -> list[str]:
    """A command that prints `stderr` and exits `code` its first `fail_times` runs, then succeeds."""
    count = tmp_path / "count"
    count.write_text("")
    script = f'echo x >> "{count}"; if [ "$(wc -l < "{count}")" -le {fail_times} ]; then echo "{stderr}" >&2; exit {code}; fi; echo done'
    return ["bash", "-c", script]


def test_run_retries_on_transient_stderr(tmp_path, capsys):
    sleeps = []
    proc = netretry.run(_command(tmp_path, "HTTP 502: Bad Gateway", 1, 2), sleep=sleeps.append)
    assert proc.returncode == 0 and proc.stdout.strip() == "done"
    assert len((tmp_path / "count").read_text().splitlines()) == 3 and len(sleeps) == 2
    assert "HTTP 502: Bad Gateway" in capsys.readouterr().err  # the failed attempts stay visible


def test_run_returns_a_permanent_failure_at_once(tmp_path):
    sleeps = []
    proc = netretry.run(_command(tmp_path, "HTTP 404: Not Found", 1, 5), sleep=sleeps.append)
    assert proc.returncode == 1 and "404" in proc.stderr
    assert len((tmp_path / "count").read_text().splitlines()) == 1 and sleeps == []


def test_run_returns_the_last_failure_when_exhausted(tmp_path):
    sleeps = []
    proc = netretry.run(_command(tmp_path, "connection reset by peer", 1, 99), attempts=3, sleep=sleeps.append)
    assert proc.returncode == 1 and "reset" in proc.stderr
    assert len((tmp_path / "count").read_text().splitlines()) == 3 and len(sleeps) == 2


# ------------------------------------------------------------------------------------- resolve_version (inlined)


def _responses(*answers):
    calls = []

    def get(url, headers=None, method="GET", timeout=60):
        calls.append(url)
        answer = answers[min(len(calls), len(answers)) - 1]
        if isinstance(answer, BaseException):
            raise answer
        return rv.HttpResponse(answer, b"{}")

    return get, calls


def test_resolve_version_retries_5xx_and_network_errors_only():
    sleeps = []
    get, calls = _responses(503, urllib.error.URLError(ConnectionResetError(104, "reset")), 200)
    assert rv._with_retries(get, sleep=sleeps.append)("https://x").status == 200
    assert len(calls) == 3 and len(sleeps) == 2
    get, calls = _responses(404, 200)
    assert rv._with_retries(get, sleep=sleeps.append)("https://x").status == 404 and len(calls) == 1
    get, calls = _responses(urllib.error.URLError(ssl.SSLCertVerificationError("bad cert")), 200)
    with pytest.raises(urllib.error.URLError):
        rv._with_retries(get, sleep=sleeps.append)("https://x")
    assert len(calls) == 1


def test_resolve_version_gives_up_with_the_last_answer(monkeypatch):
    monkeypatch.setenv("RETRY_MAX_ATTEMPTS", "3")
    sleeps = []
    get, calls = _responses(502)
    assert rv._with_retries(get, sleep=sleeps.append)("https://x").status == 502
    assert len(calls) == 3 and len(sleeps) == 2


def test_resolve_version_retries_only_inside_the_block(monkeypatch):
    """The control plane and the hub import resolve() and get no retries; the command line does."""
    monkeypatch.setenv("RETRY_BASE_DELAY", "0")
    get, calls = _responses(503, 200)
    monkeypatch.setattr(rv, "http_get", get)
    assert rv.http_get("https://x").status == 503
    calls.clear()
    with rv.retries():
        assert rv.http_get("https://x").status == 200
    assert len(calls) == 2
    assert rv.http_get is get


def test_resolve_version_stays_a_single_file():
    """It is imported on its own (control vendors it, the hub checks out only this file): no sibling imports."""
    text = Path(rv.__file__).read_text(encoding="utf-8")
    assert "import netretry" not in text and "import gha" not in text


# ------------------------------------------------------------------------------------- wired scripts


class _Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen(*answers):
    calls = []

    def urlopen(request, timeout=None):
        calls.append(request)
        answer = answers[min(len(calls), len(answers)) - 1]
        if isinstance(answer, BaseException):
            raise answer
        return _Response(answer)

    return urlopen, calls


def test_pages_fetch_retries_network_failures_and_keeps_404_as_none(monkeypatch):
    monkeypatch.setenv("RETRY_BASE_DELAY", "0")
    urlopen, calls = _urlopen(urllib.error.URLError("Failure when receiving data from the peer"), b"data")
    monkeypatch.setattr(pages.urllib.request, "urlopen", urlopen)
    assert pages.make_fetch("t")("https://x/a") == b"data" and len(calls) == 2
    urlopen, calls = _urlopen(_http_error(404))
    monkeypatch.setattr(pages.urllib.request, "urlopen", urlopen)
    assert pages.make_fetch("")("https://x/b") is None and len(calls) == 1


def test_pages_gh_json_retries_transient_gh_failures(monkeypatch):
    monkeypatch.setenv("RETRY_BASE_DELAY", "0")
    answers = iter([(1, "", "HTTP 502: Bad Gateway"), (0, '{"ok": true}', "")])

    def fake_run(cmd, **kwargs):
        code, out, err = next(answers)
        return netretry.subprocess.CompletedProcess(cmd, code, out, err)

    monkeypatch.setattr(netretry.subprocess, "run", fake_run)
    assert pages.gh_json(["repos/o/r"]) == {"ok": True}
    monkeypatch.setattr(netretry.subprocess, "run", lambda cmd, **kw: netretry.subprocess.CompletedProcess(cmd, 1, "", "HTTP 404: Not Found"))
    with pytest.raises(RuntimeError, match="404"):
        pages.gh_json(["repos/o/r"])


def test_package_versions_fetch_retries_then_gives_none(monkeypatch):
    monkeypatch.setenv("RETRY_BASE_DELAY", "0")
    urlopen, calls = _urlopen(TimeoutError("timed out"), b"NAME VERSION\n")
    monkeypatch.setattr(package_versions.urllib.request, "urlopen", urlopen)
    assert package_versions.fetch("https://x") == "NAME VERSION\n" and len(calls) == 2
    urlopen, calls = _urlopen(_http_error(404))
    monkeypatch.setattr(package_versions.urllib.request, "urlopen", urlopen)
    assert package_versions.fetch("https://x") is None and len(calls) == 1


def test_notify_retries_a_few_times_and_never_fails_the_build(monkeypatch, capsys):
    monkeypatch.setenv("RETRY_BASE_DELAY", "0")
    for k, v in {"CONTROL_URL": "https://control", "CONTROL_EVENTS_SECRET": "k", "REPOSITORY": "o/r", "RUN_ID": "1"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("BUNDLE_PATH", raising=False)
    bodies = []

    def down(url, body, signature, timeout=30):
        bodies.append(body)
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    monkeypatch.setattr(notify, "post", down)
    assert notify.main() == 0
    assert len(bodies) == notify.ATTEMPTS and len(set(bodies)) == 1  # one body: control de-duplicates repeats
    assert "could not reach the control plane" in capsys.readouterr().out

    bodies.clear()

    def refused(url, body, signature, timeout=30):
        bodies.append(body)
        raise _http_error(401)

    monkeypatch.setattr(notify, "post", refused)
    assert notify.main() == 0 and len(bodies) == 1

