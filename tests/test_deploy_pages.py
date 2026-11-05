from __future__ import annotations

import json
import urllib.error
from typing import Any

import deploy_pages
import pytest

ENV = {
    "GITHUB_SHA": "9b61b6fc",
    "GITHUB_RUN_ID": "42",
    "GITHUB_RUN_ATTEMPT": "2",
    "GITHUB_REPOSITORY": "linuxserver-playground/hub",
    "GITHUB_TOKEN": "t",
    "ACTIONS_ID_TOKEN_REQUEST_URL": "https://token.example/?x=1",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "rt",
}
API = "https://api.github.com/repos/linuxserver-playground/hub/pages/deployments"


class Fake:
    def __init__(self, statuses: list[str], created: dict[str, Any] | None = None) -> None:
        self.statuses = statuses
        self.created = (
            created if created is not None else {"status_url": f"{API}/9b61b6fc-42-2-1", "page_url": "https://x/hub/"}
        )
        self.calls: list[tuple[str, str, Any]] = []

    def __call__(self, method: str, url: str, body: Any, headers: dict[str, str]) -> dict[str, Any]:
        self.calls.append((method, url, body))
        if url.startswith("https://token.example/"):
            assert headers["Authorization"] == "bearer rt"
            return {"value": "oidc"}
        if method == "POST" and url == API:
            return self.created
        if method == "GET":
            return {"status": self.statuses.pop(0)}
        return {}


def run(fake: Fake, attempt: int = 1, clock: Any = None) -> tuple[str, str]:
    return deploy_pages.deploy(
        123, attempt, dict(ENV), request=fake, sleep=lambda _s: None, clock=clock or (lambda: 0.0)
    )


def test_each_deploy_has_its_own_build_version() -> None:
    # The whole point: never the bare commit, which Pages answers with an earlier deploy's status.
    assert deploy_pages.build_version(ENV, 1) == "9b61b6fc-42-2-1"
    assert deploy_pages.build_version(ENV, 3) == "9b61b6fc-42-2-3"
    assert deploy_pages.build_version({**ENV, "GITHUB_RUN_ATTEMPT": "3"}, 1) != deploy_pages.build_version(ENV, 1)


def test_creates_with_the_oidc_token_and_waits_for_success() -> None:
    fake = Fake(["deployment_queued", "syncing_files", "succeed"])
    assert run(fake) == ("succeed", "https://x/hub/")
    post = next(c for c in fake.calls if c[0] == "POST")
    assert post[2] == {"artifact_id": 123, "pages_build_version": "9b61b6fc-42-2-1", "oidc_token": "oidc"}
    # Status is read from the id the status_url names.
    assert [c[1] for c in fake.calls if c[0] == "GET" and c[1].startswith(API)] == [f"{API}/9b61b6fc-42-2-1"] * 3


def test_a_final_error_fails_without_a_page_url() -> None:
    fake = Fake(["deployment_queued", "deployment_failed"])
    assert run(fake) == ("deployment_failed", "")


def test_a_deploy_that_never_settles_is_cancelled() -> None:
    ticks = iter(range(0, 10_000, 100))
    fake = Fake(["updating_pages"] * 20)
    assert run(fake, clock=lambda: float(next(ticks))) == ("timed_out", "")
    assert fake.calls[-1][:2] == ("POST", f"{API}/9b61b6fc-42-2-1/cancel")


def test_the_id_from_the_response_wins() -> None:
    fake = Fake(["succeed"], created={"id": 77, "page_url": "u"})
    run(fake, attempt=2)
    assert [c[1] for c in fake.calls if c[0] == "GET" and c[1].startswith(API)] == [f"{API}/77"]


def test_main_reports_a_refusal(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def refuse(*_a: Any, **_k: Any) -> Any:
        raise urllib.error.HTTPError(API, 400, "Bad Request", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(deploy_pages, "deploy", refuse)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert deploy_pages.main(["--artifact-id", "1"]) == 1
    out = capsys.readouterr().out
    assert "HTTP 400" in out and "http_400" in out


def test_no_oidc_token_says_what_the_job_lacks() -> None:
    env = {k: v for k, v in ENV.items() if not k.startswith("ACTIONS_ID_TOKEN")}
    with pytest.raises(SystemExit, match="id-token: write"):
        deploy_pages.oidc_token(env, Fake([]))


def test_payload_is_json() -> None:
    fake = Fake(["succeed"])
    run(fake)
    json.dumps(fake.calls)
