from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

import pytest

from go_vote.export import (
    CLASSIFIER_VERSION,
    OUTPUT_FILES,
    CaptureObservation,
    _verify_readonly_connection,
    build_weekly_rows,
    collapse_partition_rows,
    create_readonly_engine,
    read_csv_files,
    read_observations,
    validate_publishable,
    write_csv_files,
)


class _Result:
    def __init__(self, rows: Sequence[Mapping[str, object]]) -> None:
        self.rows = [dict(row) for row in rows]

    def mappings(self) -> list[dict[str, object]]:
        return self.rows


def test_duplicate_ocr_rows_aliases_boundary_and_broad_vs_exact() -> None:
    joined_rows = [
        {
            "searches_id": 1,
            "captured_at": datetime(2026, 4, 30, 12),
            "raw_engine": "www.google.com",
            "ocr_row_id": 10,
            "canonical_positive": 1,
            "exact_positive": 0,
        },
        {
            "searches_id": 1,
            "captured_at": datetime(2026, 4, 30, 12),
            "raw_engine": "www.google.com",
            "ocr_row_id": 11,
            "canonical_positive": 0,
            "exact_positive": 1,
        },
        {
            "searches_id": 2,
            "captured_at": datetime(2026, 5, 1, 12),
            "raw_engine": "search.yahoo.com",
            "ocr_row_id": 12,
            "canonical_positive": 0,
            "exact_positive": 0,
        },
        {
            "searches_id": 3,
            "captured_at": datetime(2026, 5, 5, 12),
            "raw_engine": "www.yahoo.com",
            "ocr_row_id": None,
            "canonical_positive": 0,
            "exact_positive": 0,
        },
        {
            "searches_id": 4,
            "captured_at": datetime(2026, 5, 5, 13),
            "raw_engine": "www.bing.com",
            "ocr_row_id": 13,
            "canonical_positive": 1,
            "exact_positive": 0,
        },
    ]
    observations = collapse_partition_rows(joined_rows)
    assert len(observations) == 4
    assert observations[0].recorded_engine == "Google"
    assert observations[0].canonical_positive
    assert observations[0].exact_positive
    assert observations[1].recorded_engine == "Yahoo"
    assert observations[2].recorded_engine == "Yahoo"

    rows = build_weekly_rows(
        observations,
        datetime(2026, 4, 29, tzinfo=UTC),
        datetime(2026, 5, 6, tzinfo=UTC),
        CLASSIFIER_VERSION,
    )
    assert len(rows["All"]) == 2
    assert rows["All"][0]["homepage_captures"] == 2
    assert rows["All"][0]["canonical_govote_positive"] == 1
    assert rows["All"][0]["exact_go_vote_phrase_positive"] == 1
    assert rows["Yahoo"][0]["homepage_captures"] == 1
    assert rows["Yahoo"][1]["homepage_captures"] == 1
    assert rows["All"][1]["homepage_captures"] == 2
    assert rows["All"][1]["ocr_completed"] == 1


def test_zero_positive_weeks_and_combined_counts_are_emitted() -> None:
    observations = [
        CaptureObservation(1, datetime(2026, 4, 1, 12, tzinfo=UTC), "Google", True, False, False),
        CaptureObservation(2, datetime(2026, 4, 15, 12, tzinfo=UTC), "Bing", True, False, False),
    ]
    rows = build_weekly_rows(
        observations,
        datetime(2026, 4, 1, tzinfo=UTC),
        datetime(2026, 4, 20, tzinfo=UTC),
    )
    assert len(rows["All"]) == 3
    assert rows["All"][1]["homepage_captures"] == 0
    assert rows["All"][1]["canonical_govote_per_100_ocred"] == "0.000000"
    assert sum(int(str(row["homepage_captures"])) for row in rows["All"]) == 2
    assert rows["All"][0]["is_partial_week"] == "true"
    assert rows["All"][1]["is_partial_week"] == "false"


def test_unknown_engine_and_duplicate_search_fail_closed() -> None:
    with pytest.raises(ValueError, match="unknown recorded search engine"):
        collapse_partition_rows(
            [
                {
                    "searches_id": 9,
                    "captured_at": datetime(2026, 4, 1),
                    "raw_engine": "example.invalid",
                    "ocr_row_id": None,
                    "canonical_positive": 0,
                    "exact_positive": 0,
                }
            ]
        )

    observation = CaptureObservation(1, datetime(2026, 4, 1, tzinfo=UTC), "Google", True, False, False)
    with pytest.raises(ValueError, match="duplicate searches.id"):
        build_weekly_rows(
            [observation, observation],
            datetime(2026, 4, 1, tzinfo=UTC),
            datetime(2026, 4, 2, tzinfo=UTC),
        )


def test_readonly_engine_rejects_any_other_database_user() -> None:
    with pytest.raises(ValueError, match="sentiment_readonly"):
        create_readonly_engine("mysql+pymysql://writer:secret@db.kumquat-tlkxwawsjpr9iyde6takjb.cloud:25060/kumquat")
    with pytest.raises(ValueError, match="host, port, and name"):
        create_readonly_engine("mysql+pymysql://sentiment_readonly:secret@example.invalid/kumquat")
    with pytest.raises(ValueError, match="query options"):
        create_readonly_engine(
            "mysql+pymysql://sentiment_readonly:secret@"
            "db.kumquat-tlkxwawsjpr9iyde6takjb.cloud:25060/kumquat?init_command=DELETE"
        )


def test_readonly_grant_verification_rejects_write_and_grant_option() -> None:
    for unsafe_grant in (
        'GRANT SELECT, INSERT ON "kumquat".* TO "sentiment_readonly"@"%"',
        'GRANT SELECT ON "kumquat".* TO "sentiment_readonly"@"%" WITH GRANT OPTION',
        'GRANT SELECT ON "unrelated".* TO "sentiment_readonly"@"%"',
    ):
        connection = MagicMock()
        connection.exec_driver_sql.return_value.scalars.return_value = [
            'GRANT SELECT ON "kumquat".* TO "sentiment_readonly"@"%"',
            unsafe_grant,
        ]
        with pytest.raises(ValueError, match="unexpected grant or scope"):
            _verify_readonly_connection(connection, require_tls=False)


def test_read_observations_pages_inside_each_yrmo() -> None:
    april_rows = [
        {
            "searches_id": index,
            "captured_at": datetime(2026, 4, 30, 12),
            "raw_engine": "www.google.com",
            "ocr_row_id": index,
            "canonical_positive": 0,
            "exact_positive": 0,
        }
        for index in range(1, 2001)
    ]
    april_rows.append(dict(april_rows[-1], ocr_row_id=99999, canonical_positive=1))
    april_last = [
        {
            "searches_id": 2001,
            "captured_at": datetime(2026, 4, 30, 13),
            "raw_engine": "www.bing.com",
            "ocr_row_id": None,
            "canonical_positive": 0,
            "exact_positive": 0,
        }
    ]
    may_rows = [
        {
            "searches_id": 3000,
            "captured_at": datetime(2026, 5, 1, 12),
            "raw_engine": "search.yahoo.com",
            "ocr_row_id": 3000,
            "canonical_positive": 0,
            "exact_positive": 1,
        }
    ]
    connection = MagicMock()
    cipher_result = MagicMock()
    cipher_result.one.return_value = ("Ssl_cipher", "TLS_AES_256_GCM_SHA384")
    grants_result = MagicMock()
    grants_result.scalars.return_value = [
        'GRANT USAGE ON *.* TO "sentiment_readonly"@"%"',
        'GRANT SELECT ON "kumquat".* TO "sentiment_readonly"@"%"',
    ]
    connection.exec_driver_sql.side_effect = [cipher_result, grants_result, MagicMock()]
    connection.execute.side_effect = [_Result(april_rows), _Result(april_last), _Result(may_rows)]
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = connection

    observations = read_observations(
        datetime(2026, 4, 29, tzinfo=UTC),
        datetime(2026, 5, 2, tzinfo=UTC),
        engine=engine,
    )

    assert len(observations) == 2002
    assert next(item for item in observations if item.searches_id == 2000).canonical_positive
    assert next(item for item in observations if item.searches_id == 3000).recorded_engine == "Yahoo"
    assert connection.execute.call_count == 3
    parameters = [current.args[1] for current in connection.execute.call_args_list]
    assert [item["yrmo"] for item in parameters] == ["202604", "202604", "202605"]
    assert [item["after_id"] for item in parameters] == [0, 2000, 0]
    engine.dispose.assert_not_called()


def test_write_csv_files_is_complete_and_cleans_up_after_failure() -> None:
    rows = build_weekly_rows(
        [],
        datetime(2026, 4, 1, tzinfo=UTC),
        datetime(2026, 4, 2, tzinfo=UTC),
    )
    with TemporaryDirectory() as temporary:
        output = Path(temporary) / "success"
        paths = write_csv_files(rows, output)
        assert {path.name for path in paths} == set(OUTPUT_FILES.values())
        assert all(path.read_text(encoding="utf-8").startswith("week_start_utc,") for path in paths)

        broken_output = Path(temporary) / "failure"
        broken_rows = dict(rows)
        del broken_rows["Yahoo"]
        with pytest.raises(KeyError):
            write_csv_files(broken_rows, broken_output)
        assert not broken_output.exists()


def test_csv_round_trip_and_non_regression_gate() -> None:
    baseline_rows = build_weekly_rows(
        [CaptureObservation(1, datetime(2026, 4, 1, tzinfo=UTC), "Google", True, True, False)],
        datetime(2026, 4, 1, tzinfo=UTC),
        datetime(2026, 4, 2, tzinfo=UTC),
    )
    candidate_rows = build_weekly_rows(
        [
            CaptureObservation(1, datetime(2026, 4, 1, tzinfo=UTC), "Google", True, True, False),
            CaptureObservation(2, datetime(2026, 4, 1, tzinfo=UTC), "Bing", True, False, True),
        ],
        datetime(2026, 4, 1, tzinfo=UTC),
        datetime(2026, 4, 3, tzinfo=UTC),
    )
    validate_publishable(candidate_rows, baseline_rows)

    with TemporaryDirectory() as temporary:
        directory = Path(temporary) / "candidate"
        write_csv_files(candidate_rows, directory)
        loaded = read_csv_files(directory)
        validate_publishable(loaded, baseline_rows)

    regressed_rows = deepcopy(candidate_rows)
    regressed_rows["Bing"][0]["homepage_captures"] = 0
    regressed_rows["Bing"][0]["ocr_completed"] = 0
    regressed_rows["Bing"][0]["exact_go_vote_phrase_positive"] = 0
    regressed_rows["Bing"][0]["ocr_coverage_pct"] = "0.000000"
    regressed_rows["Bing"][0]["exact_go_vote_phrase_per_100_homepages"] = "0.000000"
    regressed_rows["Bing"][0]["exact_go_vote_phrase_per_100_ocred"] = "0.000000"
    regressed_rows["All"][0]["homepage_captures"] = 1
    regressed_rows["All"][0]["ocr_completed"] = 1
    regressed_rows["All"][0]["exact_go_vote_phrase_positive"] = 0
    regressed_rows["All"][0]["canonical_govote_per_100_homepages"] = "100.000000"
    regressed_rows["All"][0]["canonical_govote_per_100_ocred"] = "100.000000"
    regressed_rows["All"][0]["exact_go_vote_phrase_per_100_homepages"] = "0.000000"
    regressed_rows["All"][0]["exact_go_vote_phrase_per_100_ocred"] = "0.000000"
    with pytest.raises(ValueError, match="regressed homepage_captures"):
        validate_publishable(regressed_rows, candidate_rows)


def test_publish_gate_rejects_empty_aggregate() -> None:
    empty = build_weekly_rows(
        [],
        datetime(2026, 4, 1, tzinfo=UTC),
        datetime(2026, 4, 2, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="no homepage captures"):
        validate_publishable(empty)


def test_publish_gate_rejects_bad_rates_bounds_and_metadata() -> None:
    rows = build_weekly_rows(
        [CaptureObservation(1, datetime(2026, 4, 1, tzinfo=UTC), "Google", True, True, False)],
        datetime(2026, 4, 1, tzinfo=UTC),
        datetime(2026, 4, 2, tzinfo=UTC),
    )

    bad_rate = deepcopy(rows)
    bad_rate["Google"][0]["canonical_govote_per_100_homepages"] = "99.000000"
    with pytest.raises(ValueError, match="incorrect canonical_govote_per_100_homepages"):
        validate_publishable(bad_rate)

    bad_bounds = deepcopy(rows)
    bad_bounds["Google"][0]["ocr_completed"] = 2
    bad_bounds["All"][0]["ocr_completed"] = 2
    with pytest.raises(ValueError, match="invalid count bounds"):
        validate_publishable(bad_bounds)

    bad_metadata = deepcopy(rows)
    bad_metadata["Bing"][0]["classifier_version"] = "unknown"
    with pytest.raises(ValueError, match="do not share one snapshot cutoff"):
        validate_publishable(bad_metadata)

    bad_temporal = deepcopy(rows)
    bad_temporal["Bing"][0]["week_end_utc"] = "2026-04-05T00:00:00Z"
    with pytest.raises(ValueError, match="temporal metadata is not aligned"):
        validate_publishable(bad_temporal)
