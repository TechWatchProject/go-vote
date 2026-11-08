"""The report is published to nexus behind GitHub sign-in, never to GitHub Pages."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from go_vote import publish
from go_vote.export import OUTPUT_FILES

ROOT = Path(__file__).resolve().parents[1]
FROZEN_REPORT = ROOT / "reports" / "2026-04-01_to_2026-08-06T051200Z"


class Recorder:
    def __init__(self, status: int = 204) -> None:
        self.status = status
        self.requests: list[Any] = []

    def __call__(self, request: Any, timeout: float) -> Any:
        self.requests.append(request)
        recorder = self

        class Response:
            status = recorder.status

            def __enter__(self) -> Any:
                return self

            def __exit__(self, *args: object) -> None:
                return None

        return Response()


def test_bundle_carries_exactly_the_four_csvs() -> None:
    bundle = publish.build_bundle(FROZEN_REPORT, generated_at="2026-11-07T00:00:00Z")
    assert bundle["generated_at"] == "2026-11-07T00:00:00Z"
    assert set(bundle["files"]) == set(OUTPUT_FILES.values())
    for name, text in bundle["files"].items():
        assert text == (FROZEN_REPORT / name).read_text(encoding="utf-8")


def test_bundle_refuses_a_missing_csv(tmp_path: Path) -> None:
    (tmp_path / "go-vote-google-weekly.csv").write_text("x\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        publish.build_bundle(tmp_path, generated_at="now")


def test_send_posts_json_with_the_bearer_token() -> None:
    opener = Recorder()
    publish.send({"generated_at": "now", "files": {}}, token="tok", url=publish.DEFAULT_URL, opener=opener)
    (request,) = opener.requests
    assert request.full_url == "https://kumquat.link/dashboards-ingest/go-vote"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer tok"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == {"generated_at": "now", "files": {}}


def test_a_non_204_answer_fails() -> None:
    with pytest.raises(RuntimeError):
        publish.send({}, token="tok", url=publish.DEFAULT_URL, opener=Recorder(status=200))


@pytest.mark.parametrize(
    "url",
    [
        "http://kumquat.link/dashboards-ingest/go-vote",
        "https://techwatchproject.github.io/go-vote/",
        "https://kumquat.link.evil.example/dashboards-ingest/go-vote",
    ],
)
def test_only_the_nexus_https_route_is_accepted(url: str) -> None:
    with pytest.raises(ValueError):
        publish.checked_url(url)


def test_main_requires_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GO_VOTE_INGEST_TOKEN", raising=False)
    assert publish.main(["--data-dir", str(FROZEN_REPORT)]) == 2


def test_main_publishes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GO_VOTE_INGEST_TOKEN", "tok")
    opener = Recorder()
    monkeypatch.setattr(publish, "urlopen", opener)
    assert publish.main(["--data-dir", str(FROZEN_REPORT)]) == 0
    assert len(opener.requests) == 1
