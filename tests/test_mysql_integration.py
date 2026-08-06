from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

import go_vote.export as exporter

ROOT_DSN = os.environ.get("GO_VOTE_TEST_MYSQL_ROOT_DSN")
EXPECTED_PORT = int(os.environ.get("GO_VOTE_TEST_MYSQL_EXPECTED_PORT", "3306"))
pytestmark = pytest.mark.skipif(not ROOT_DSN, reason="GO_VOTE_TEST_MYSQL_ROOT_DSN is not set")


def test_mysql_regex_pagination_and_server_readonly(monkeypatch: pytest.MonkeyPatch) -> None:
    assert ROOT_DSN is not None
    root_url = make_url(ROOT_DSN)
    if (
        root_url.drivername != "mysql+pymysql"
        or root_url.host not in {"127.0.0.1", "localhost"}
        or root_url.database != "go_vote_test"
        or root_url.username != "root"
        or root_url.port != EXPECTED_PORT
        or root_url.query
    ):
        pytest.fail(
            "GO_VOTE_TEST_MYSQL_ROOT_DSN must target the expected local go_vote_test port without query options"
        )
    admin = create_engine(ROOT_DSN)
    with admin.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS homepage_ocr_text"))
        connection.execute(text("DROP TABLE IF EXISTS searches"))
        connection.execute(
            text(
                "CREATE TABLE searches ("
                "id BIGINT PRIMARY KEY, datetime DATETIME NOT NULL, engine VARCHAR(100) NOT NULL, "
                "is_homepage BOOLEAN NOT NULL, screenshot VARCHAR(255), yrmo VARCHAR(6) NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE homepage_ocr_text ("
                "id BIGINT PRIMARY KEY, searches_id BIGINT NOT NULL, vote BOOLEAN, ocr_text TEXT)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO searches (id, datetime, engine, is_homepage, screenshot, yrmo) VALUES "
                "(1, '2026-04-01 00:00:00', 'www.google.com', 1, '1.webp', '202604'),"
                "(2, '2026-04-02 00:00:00', 'www.bing.com', 1, '2.webp', '202604'),"
                "(3, '2026-04-03 00:00:00', 'search.yahoo.com', 1, '3.webp', '202604')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO homepage_ocr_text (id, searches_id, vote, ocr_text) VALUES "
                "(10, 1, 1, 'Please GO   VOTE today.'),"
                "(11, 1, 0, 'duplicate row'),"
                "(12, 2, 0, 'undergo voter registration'),"
                "(13, 3, 0, NULL)"
            )
        )
        connection.execute(text("CREATE USER IF NOT EXISTS 'sentiment_readonly'@'%' IDENTIFIED BY 'readonly'"))
        connection.execute(text("ALTER USER 'sentiment_readonly'@'%' IDENTIFIED BY 'readonly'"))
        connection.execute(text("GRANT SELECT ON go_vote_test.* TO 'sentiment_readonly'@'%'"))
    admin.dispose()

    readonly_dsn = root_url.set(username="sentiment_readonly", password="readonly")
    engine = exporter.create_readonly_engine(
        readonly_dsn.render_as_string(hide_password=False),
        expected_host=str(root_url.host),
        expected_port=int(root_url.port or 3306),
        expected_database=str(root_url.database),
        require_tls=False,
    )
    monkeypatch.setattr(exporter, "PAGE_SIZE", 2)
    observations = exporter.read_observations(
        datetime(2026, 4, 1, tzinfo=UTC),
        datetime(2026, 5, 1, tzinfo=UTC),
        engine=engine,
        require_tls=False,
        allowed_grant_schemas=frozenset({"go_vote_test"}),
        required_grant_schema="go_vote_test",
    )
    assert len(observations) == 3
    assert observations[0].canonical_positive
    assert observations[0].exact_positive
    assert not observations[1].exact_positive

    with engine.connect() as connection, pytest.raises(DBAPIError):
        connection.execute(
            text(
                "INSERT INTO searches (id, datetime, engine, is_homepage, screenshot, yrmo) "
                "VALUES (99, '2026-04-04 00:00:00', 'www.google.com', 1, '99.webp', '202604')"
            )
        )
    engine.dispose()
