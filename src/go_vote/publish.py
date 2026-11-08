"""Publish the weekly CSVs to nexus, where they are readable only behind GitHub sign-in.

The report used to be committed to `docs/` and served by GitHub Pages, which is
public. It now goes to `https://kumquat.link/dashboards/go-vote/`. The page ships
in the QA repo (`qa/dashboard_static/go-vote/`). This job can only replace the
data, using its own `GO_VOTE_INGEST_TOKEN`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from go_vote.export import OUTPUT_FILES

DEFAULT_URL = "https://kumquat.link/dashboards-ingest/go-vote"
TOKEN_ENV = "GO_VOTE_INGEST_TOKEN"


def checked_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "kumquat.link":
        raise ValueError(f"refusing to publish outside https://kumquat.link: {url}")
    return url


def build_bundle(data_dir: Path, *, generated_at: str) -> dict[str, Any]:
    files = {name: (data_dir / name).read_text(encoding="utf-8") for name in OUTPUT_FILES.values()}
    return {"generated_at": generated_at, "files": files}


def send(bundle: dict[str, Any], *, token: str, url: str, opener: Any = None) -> None:
    request = Request(
        checked_url(url),
        data=json.dumps(bundle).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with (opener or urlopen)(request, timeout=60) as response:
        if response.status != 204:
            raise RuntimeError(f"nexus answered {response.status}, expected 204")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish GO Vote CSVs to the private nexus dashboard")
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--url", default=os.environ.get("GO_VOTE_INGEST_URL") or DEFAULT_URL)
    args = parser.parse_args(argv)
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        print(f"{TOKEN_ENV} is not set", file=sys.stderr)
        return 2
    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    send(build_bundle(args.data_dir, generated_at=generated_at), token=token, url=args.url, opener=urlopen)
    print("published GO Vote CSVs to nexus")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
