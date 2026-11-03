"""Optional read-only production SQL integration; no writable fixture DSN."""

import os
from datetime import UTC, datetime, timedelta

import pytest

from go_vote.export import create_readonly_engine, read_observations

READONLY_DSN = os.environ.get("GO_VOTE_TEST_READONLY_DSN")


@pytest.mark.skipif(not READONLY_DSN, reason="GO_VOTE_TEST_READONLY_DSN is not set")
def test_mysql_html_and_ocr_join_is_readonly_and_deduplicated():
    assert READONLY_DSN is not None
    engine = create_readonly_engine(READONLY_DSN)
    end = datetime.now(UTC)
    try:
        observations = read_observations(end - timedelta(days=1), end, engine=engine)
        assert len({row.searches_id for row in observations}) == len(observations)
        assert all(not row.html_govote_positive or row.html_analyzed for row in observations)
        assert all(not row.canonical_positive or row.ocr_completed for row in observations)
    finally:
        engine.dispose()
