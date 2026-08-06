"""Read-only weekly GO Vote aggregate exporter.

The query never returns OCR text. Duplicate OCR rows are collapsed by
``searches.id`` before counts are computed, and only aggregate CSVs are
written.
"""

from __future__ import annotations

import _thread
import argparse
import csv
import os
import re
import ssl
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine, make_url

CSV_COLUMNS = (
    "week_start_utc",
    "week_end_utc",
    "recorded_engine",
    "is_partial_week",
    "homepage_captures",
    "ocr_completed",
    "ocr_coverage_pct",
    "canonical_govote_positive",
    "canonical_govote_per_100_homepages",
    "canonical_govote_per_100_ocred",
    "exact_go_vote_phrase_positive",
    "exact_go_vote_phrase_per_100_homepages",
    "exact_go_vote_phrase_per_100_ocred",
    "snapshot_cutoff_utc",
    "classifier_version",
)
CLASSIFIER_VERSION = "canonical-vote-election-poll-v1+exact-go-vote-v1"
EXACT_GO_VOTE_PATTERN = r"(^|[^[:alnum:]_])go[[:space:]]+vote([^[:alnum:]_]|$)"
PAGE_SIZE = 2_000
READONLY_USERNAME = "sentiment_readonly"
PRODUCTION_DATABASE_HOST = "db.kumquat-tlkxwawsjpr9iyde6takjb.cloud"
PRODUCTION_DATABASE_PORT = 25060
PRODUCTION_DATABASE_NAME = "kumquat"
PROJECT_CA_CERTIFICATE = Path(__file__).with_name("kumquat-project-ca.pem")
PRODUCTION_READONLY_SCHEMAS = frozenset({"kumquat", "defaultdb"})
REPORT_START = datetime(2026, 4, 1, tzinfo=UTC)
ENGINE_LABELS = {
    "www.google.com": "Google",
    "www.bing.com": "Bing",
    "search.yahoo.com": "Yahoo",
    "www.yahoo.com": "Yahoo",
}
OUTPUT_FILES = {
    "Google": "go-vote-google-weekly.csv",
    "Bing": "go-vote-bing-weekly.csv",
    "Yahoo": "go-vote-yahoo-weekly.csv",
    "All": "go-vote-all-engines-weekly.csv",
}

PARTITION_SQL = text(
    """
    SELECT
        s.id AS searches_id,
        s.datetime AS captured_at,
        s.engine AS raw_engine,
        hot.id AS ocr_row_id,
        CASE WHEN hot.vote = 1 THEN 1 ELSE 0 END AS canonical_positive,
        CASE
            WHEN LOWER(COALESCE(hot.ocr_text, '')) REGEXP :exact_pattern THEN 1
            ELSE 0
        END AS exact_positive
    FROM searches AS s
    INNER JOIN (
        SELECT id
        FROM searches
        WHERE yrmo = :yrmo
          AND datetime >= :partition_start
          AND datetime < :partition_end
          AND is_homepage = 1
          AND screenshot IS NOT NULL
          AND TRIM(screenshot) <> ''
          AND id > :after_id
        ORDER BY id
        LIMIT :page_size
    ) AS page ON page.id = s.id
    LEFT JOIN homepage_ocr_text AS hot ON hot.searches_id = s.id
    ORDER BY s.id, hot.id
    """
)


@dataclass(frozen=True)
class CaptureObservation:
    searches_id: int
    captured_at: datetime
    recorded_engine: str
    ocr_completed: bool
    canonical_positive: bool
    exact_positive: bool


@dataclass
class _MutableObservation:
    searches_id: int
    captured_at: datetime
    recorded_engine: str
    ocr_completed: bool = False
    canonical_positive: bool = False
    exact_positive: bool = False


@dataclass
class _Counts:
    homepage_captures: int = 0
    ocr_completed: int = 0
    canonical_positive: int = 0
    exact_positive: int = 0

    def add(self, observation: CaptureObservation) -> None:
        self.homepage_captures += 1
        self.ocr_completed += int(observation.ocr_completed)
        self.canonical_positive += int(observation.canonical_positive)
        self.exact_positive += int(observation.exact_positive)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso_utc(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_engine_label(raw_engine: str) -> str:
    try:
        return ENGINE_LABELS[raw_engine]
    except KeyError as exc:
        raise ValueError(f"unknown recorded search engine: {raw_engine!r}") from exc


def collapse_partition_rows(rows: Iterable[Mapping[Any, Any]]) -> list[CaptureObservation]:
    """Collapse duplicate OCR rows into one observation per distinct search."""
    captures: dict[int, _MutableObservation] = {}
    for row in rows:
        searches_id = int(row["searches_id"])
        captured_at = _as_utc(row["captured_at"])
        engine = _canonical_engine_label(str(row["raw_engine"]))
        existing = captures.get(searches_id)
        if existing is None:
            existing = _MutableObservation(searches_id, captured_at, engine)
            captures[searches_id] = existing
        elif existing.captured_at != captured_at or existing.recorded_engine != engine:
            raise ValueError(f"conflicting rows for searches.id={searches_id}")

        has_ocr = row.get("ocr_row_id") is not None
        existing.ocr_completed = existing.ocr_completed or has_ocr
        existing.canonical_positive = existing.canonical_positive or bool(row.get("canonical_positive"))
        existing.exact_positive = existing.exact_positive or bool(row.get("exact_positive"))

    observations = [
        CaptureObservation(
            value.searches_id,
            value.captured_at,
            value.recorded_engine,
            value.ocr_completed,
            value.canonical_positive,
            value.exact_positive,
        )
        for value in captures.values()
    ]
    for observation in observations:
        if (observation.canonical_positive or observation.exact_positive) and not observation.ocr_completed:
            raise ValueError(f"positive classifier result without OCR for searches.id={observation.searches_id}")
    return sorted(observations, key=lambda item: item.searches_id)


def _month_starts(start: datetime, end: datetime) -> Iterator[datetime]:
    cursor = datetime(start.year, start.month, 1, tzinfo=UTC)
    while cursor < end:
        yield cursor
        cursor = _next_month(cursor)


def _next_month(value: datetime) -> datetime:
    if value.month == 12:
        return datetime(value.year + 1, 1, 1, tzinfo=UTC)
    return datetime(value.year, value.month + 1, 1, tzinfo=UTC)


def create_readonly_engine(
    dsn: str,
    *,
    expected_host: str = PRODUCTION_DATABASE_HOST,
    expected_port: int = PRODUCTION_DATABASE_PORT,
    expected_database: str = PRODUCTION_DATABASE_NAME,
    require_tls: bool = True,
) -> Engine:
    """Create an engine only for the expected read-only database identity."""
    url = make_url(dsn)
    if url.drivername != "mysql+pymysql":
        raise ValueError("database driver must be 'mysql+pymysql'")
    if url.query:
        raise ValueError("database DSN query options are not allowed")
    if url.username != READONLY_USERNAME:
        raise ValueError(f"database username must be {READONLY_USERNAME!r}")
    if url.host != expected_host or url.port != expected_port or url.database != expected_database:
        raise ValueError("database host, port, and name must match the approved read-only target")
    if not url.password:
        raise ValueError("database password is required")
    connect_args: dict[str, object] = {}
    if require_tls:
        context = ssl.create_default_context(cafile=str(PROJECT_CA_CERTIFICATE))
        # The approved custom host is an A record for the DigitalOcean cluster,
        # while the private project CA signs the provider hostname. Pinning the
        # exact host above and the project CA here authenticates that cluster.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_REQUIRED
        # DigitalOcean's project CA predates OpenSSL's strict requirement that
        # the CA Basic Constraints extension itself be marked critical.
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        connect_args["ssl"] = context
    return create_engine(
        url,
        pool_pre_ping=True,
        isolation_level="REPEATABLE READ",
        connect_args=connect_args,
    )


def _verify_readonly_connection(
    connection: Connection,
    *,
    require_tls: bool,
    allowed_schemas: frozenset[str] = PRODUCTION_READONLY_SCHEMAS,
    required_schema: str = PRODUCTION_DATABASE_NAME,
) -> None:
    if require_tls:
        cipher_row = connection.exec_driver_sql("SHOW STATUS LIKE 'Ssl_cipher'").one()
        if len(cipher_row) < 2 or not str(cipher_row[1]).strip():
            raise ValueError("database connection is not using TLS")
    grants = [str(grant) for grant in connection.exec_driver_sql("SHOW GRANTS").scalars()]
    usage = re.compile(r"^GRANT USAGE ON \*\.\* TO .+$")
    schema_pattern = "|".join(re.escape(schema) for schema in sorted(allowed_schemas))
    select = re.compile(rf'^GRANT SELECT ON [`"]?({schema_pattern})[`"]?\.\* TO .+$')
    selected_schemas = {match.group(1) for grant in grants if (match := select.fullmatch(grant)) is not None}
    if required_schema not in selected_schemas:
        raise ValueError(f"database account has no SELECT grant on {required_schema}")
    if any(
        "WITH GRANT OPTION" in grant or (not usage.fullmatch(grant) and not select.fullmatch(grant)) for grant in grants
    ):
        raise ValueError("database account has an unexpected grant or scope")


def read_observations(
    start: datetime,
    snapshot_cutoff: datetime,
    *,
    engine: Engine | None = None,
    require_tls: bool = True,
    allowed_grant_schemas: frozenset[str] = PRODUCTION_READONLY_SCHEMAS,
    required_grant_schema: str = PRODUCTION_DATABASE_NAME,
) -> list[CaptureObservation]:
    """Read captures one indexed month and one bounded ID page at a time."""
    start = _as_utc(start)
    snapshot_cutoff = _as_utc(snapshot_cutoff)
    if start >= snapshot_cutoff:
        raise ValueError("start must be earlier than snapshot cutoff")

    owns_engine = engine is None
    if engine is None:
        dsn = os.environ.get("KUMQUAT_READONLY_DSN", "")
        if not dsn:
            raise ValueError("KUMQUAT_READONLY_DSN is required")
        engine = create_readonly_engine(dsn)

    all_observations: dict[int, CaptureObservation] = {}
    try:
        with engine.connect() as connection:
            _verify_readonly_connection(
                connection,
                require_tls=require_tls,
                allowed_schemas=allowed_grant_schemas,
                required_schema=required_grant_schema,
            )
            connection.rollback()
            connection.exec_driver_sql("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
            try:
                for month_start in _month_starts(start, snapshot_cutoff):
                    partition_start = max(start, month_start)
                    partition_end = min(snapshot_cutoff, _next_month(month_start))
                    after_id = 0
                    while True:
                        result = connection.execute(
                            PARTITION_SQL,
                            {
                                "yrmo": month_start.strftime("%Y%m"),
                                "partition_start": partition_start.replace(tzinfo=None),
                                "partition_end": partition_end.replace(tzinfo=None),
                                "after_id": after_id,
                                "page_size": PAGE_SIZE,
                                "exact_pattern": EXACT_GO_VOTE_PATTERN,
                            },
                        )
                        page = collapse_partition_rows(result.mappings())
                        if not page:
                            break
                        for observation in page:
                            if observation.searches_id in all_observations:
                                raise ValueError(
                                    f"searches.id={observation.searches_id} appeared in multiple yrmo partitions"
                                )
                            all_observations[observation.searches_id] = observation
                        after_id = max(observation.searches_id for observation in page)
                        if len(page) < PAGE_SIZE:
                            break
            finally:
                connection.rollback()
    finally:
        if owns_engine:
            engine.dispose()
    return sorted(all_observations.values(), key=lambda item: (item.captured_at, item.searches_id))


def _week_start(value: datetime) -> datetime:
    value = _as_utc(value)
    midnight = value.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=midnight.weekday())


def _rate(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "0.000000"
    return f"{numerator * 100 / denominator:.6f}"


def build_weekly_rows(
    observations: Sequence[CaptureObservation],
    start: datetime,
    snapshot_cutoff: datetime,
    classifier_version: str = CLASSIFIER_VERSION,
) -> dict[str, list[dict[str, object]]]:
    """Aggregate distinct observations into complete Monday-based UTC weeks."""
    start = _as_utc(start)
    snapshot_cutoff = _as_utc(snapshot_cutoff)
    if start >= snapshot_cutoff:
        raise ValueError("start must be earlier than snapshot cutoff")

    week_starts: list[datetime] = []
    cursor = _week_start(start)
    while cursor < snapshot_cutoff:
        week_starts.append(cursor)
        cursor += timedelta(days=7)

    counts = {(engine, week): _Counts() for engine in OUTPUT_FILES for week in week_starts}
    seen_ids: set[int] = set()
    for observation in observations:
        if observation.searches_id in seen_ids:
            raise ValueError(f"duplicate searches.id={observation.searches_id} after OCR collapse")
        seen_ids.add(observation.searches_id)
        if not start <= observation.captured_at < snapshot_cutoff:
            raise ValueError(f"capture searches.id={observation.searches_id} is outside the requested range")
        if observation.recorded_engine not in {"Google", "Bing", "Yahoo"}:
            raise ValueError(f"unknown canonical recorded engine: {observation.recorded_engine!r}")
        week = _week_start(observation.captured_at)
        counts[(observation.recorded_engine, week)].add(observation)
        counts[("All", week)].add(observation)

    output: dict[str, list[dict[str, object]]] = {}
    cutoff_text = _iso_utc(snapshot_cutoff)
    for engine in OUTPUT_FILES:
        output[engine] = []
        for week in week_starts:
            week_end = week + timedelta(days=7)
            values = counts[(engine, week)]
            output[engine].append(
                {
                    "week_start_utc": _iso_utc(week),
                    "week_end_utc": _iso_utc(week_end),
                    "recorded_engine": engine,
                    "is_partial_week": str(week < start or week_end > snapshot_cutoff).lower(),
                    "homepage_captures": values.homepage_captures,
                    "ocr_completed": values.ocr_completed,
                    "ocr_coverage_pct": _rate(values.ocr_completed, values.homepage_captures),
                    "canonical_govote_positive": values.canonical_positive,
                    "canonical_govote_per_100_homepages": _rate(values.canonical_positive, values.homepage_captures),
                    "canonical_govote_per_100_ocred": _rate(values.canonical_positive, values.ocr_completed),
                    "exact_go_vote_phrase_positive": values.exact_positive,
                    "exact_go_vote_phrase_per_100_homepages": _rate(values.exact_positive, values.homepage_captures),
                    "exact_go_vote_phrase_per_100_ocred": _rate(values.exact_positive, values.ocr_completed),
                    "snapshot_cutoff_utc": cutoff_text,
                    "classifier_version": classifier_version,
                }
            )
    validate_reconciliation(output)
    return output


def validate_reconciliation(rows_by_engine: Mapping[str, Sequence[Mapping[str, object]]]) -> None:
    """Prove each combined weekly count equals the three engine counts."""
    individual = ("Google", "Bing", "Yahoo")
    count_columns = (
        "homepage_captures",
        "ocr_completed",
        "canonical_govote_positive",
        "exact_go_vote_phrase_positive",
    )
    lengths = {len(rows_by_engine[engine]) for engine in (*individual, "All")}
    if len(lengths) != 1:
        raise ValueError("engine CSVs do not contain the same weeks")
    for index, combined in enumerate(rows_by_engine["All"]):
        for column in count_columns:
            expected = sum(int(str(rows_by_engine[engine][index][column])) for engine in individual)
            if int(str(combined[column])) != expected:
                raise ValueError(f"combined {column} does not reconcile for week index {index}")


def read_csv_files(directory: Path) -> dict[str, list[dict[str, object]]]:
    """Read and validate the four public CSVs from a directory."""
    rows_by_engine: dict[str, list[dict[str, object]]] = {}
    for engine, filename in OUTPUT_FILES.items():
        path = directory / filename
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != CSV_COLUMNS:
                raise ValueError(f"unexpected CSV schema in {path}")
            rows = [dict(row) for row in reader]
        if any(row["recorded_engine"] != engine for row in rows):
            raise ValueError(f"unexpected recorded engine in {path}")
        rows_by_engine[engine] = rows
    validate_reconciliation(rows_by_engine)
    return rows_by_engine


def validate_publishable(
    candidate: Mapping[str, Sequence[Mapping[str, object]]],
    baseline: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
) -> None:
    """Reject empty, misaligned, stale, or regressing aggregate output."""
    validate_reconciliation(candidate)
    combined = candidate["All"]
    if not combined or sum(int(str(row["homepage_captures"])) for row in combined) == 0:
        raise ValueError("candidate contains no homepage captures")
    if sum(int(str(row["ocr_completed"])) for row in combined) == 0:
        raise ValueError("candidate contains no completed OCR")

    expected_temporal = [(row["week_start_utc"], row["week_end_utc"], row["is_partial_week"]) for row in combined]
    for engine in OUTPUT_FILES:
        temporal = [(row["week_start_utc"], row["week_end_utc"], row["is_partial_week"]) for row in candidate[engine]]
        if temporal != expected_temporal:
            raise ValueError(f"candidate temporal metadata is not aligned for {engine}")

    parsed_weeks: list[datetime] = []
    metadata: set[tuple[str, str]] = set()
    for index, row in enumerate(combined):
        week_start = _parse_utc(str(row["week_start_utc"]))
        week_end = _parse_utc(str(row["week_end_utc"]))
        if week_start.weekday() != 0 or week_start.time() != datetime.min.time():
            raise ValueError(f"week index {index} does not start Monday at 00:00 UTC")
        if week_end != week_start + timedelta(days=7):
            raise ValueError(f"week index {index} does not span exactly seven days")
        if parsed_weeks and week_start != parsed_weeks[-1] + timedelta(days=7):
            raise ValueError(f"week index {index} is not contiguous")
        parsed_weeks.append(week_start)

    for engine in OUTPUT_FILES:
        for index, row in enumerate(candidate[engine]):
            if row["recorded_engine"] != engine:
                raise ValueError(f"unexpected recorded engine for {engine} at week index {index}")
            if str(row["is_partial_week"]) not in {"true", "false"}:
                raise ValueError(f"invalid partial-week flag for {engine} at week index {index}")
            homepage = int(str(row["homepage_captures"]))
            ocr = int(str(row["ocr_completed"]))
            canonical = int(str(row["canonical_govote_positive"]))
            exact = int(str(row["exact_go_vote_phrase_positive"]))
            if not (0 <= ocr <= homepage and 0 <= canonical <= ocr and 0 <= exact <= ocr):
                raise ValueError(f"invalid count bounds for {engine} at week index {index}")
            expected_rates = {
                "ocr_coverage_pct": _rate(ocr, homepage),
                "canonical_govote_per_100_homepages": _rate(canonical, homepage),
                "canonical_govote_per_100_ocred": _rate(canonical, ocr),
                "exact_go_vote_phrase_per_100_homepages": _rate(exact, homepage),
                "exact_go_vote_phrase_per_100_ocred": _rate(exact, ocr),
            }
            for column, expected in expected_rates.items():
                if str(row[column]) != expected:
                    raise ValueError(f"incorrect {column} for {engine} at week index {index}")
            row_cutoff = str(row["snapshot_cutoff_utc"])
            classifier = str(row["classifier_version"])
            metadata.add((row_cutoff, classifier))

    if len(metadata) != 1:
        raise ValueError("candidate rows do not share one snapshot cutoff and classifier version")
    cutoff_text, classifier = next(iter(metadata))
    snapshot_cutoff = _parse_utc(cutoff_text)
    if classifier != CLASSIFIER_VERSION:
        raise ValueError(f"unexpected classifier version: {classifier!r}")
    if not parsed_weeks[0] <= snapshot_cutoff <= parsed_weeks[-1] + timedelta(days=7):
        raise ValueError("snapshot cutoff is outside the candidate week range")
    if parsed_weeks[0] != _week_start(REPORT_START) or snapshot_cutoff <= REPORT_START:
        raise ValueError("candidate does not begin at the fixed report start")
    for index, row in enumerate(combined):
        week_end = parsed_weeks[index] + timedelta(days=7)
        expected_partial = parsed_weeks[index] < REPORT_START or week_end > snapshot_cutoff
        if str(row["is_partial_week"]) != str(expected_partial).lower():
            raise ValueError(f"incorrect partial-week flag at week index {index}")

    if baseline is None:
        return
    validate_publishable(baseline)
    monotonic_columns = (
        "homepage_captures",
        "ocr_completed",
        "canonical_govote_positive",
        "exact_go_vote_phrase_positive",
    )
    for engine in OUTPUT_FILES:
        candidate_by_week = {str(row["week_start_utc"]): row for row in candidate[engine]}
        for old_row in baseline[engine]:
            week = str(old_row["week_start_utc"])
            new_row = candidate_by_week.get(week)
            if new_row is None:
                raise ValueError(f"candidate dropped baseline week {week} for {engine}")
            for column in monotonic_columns:
                if int(str(new_row[column])) < int(str(old_row[column])):
                    raise ValueError(f"candidate regressed {column} for {engine} in week {week}")
        old_cutoff = max((str(row["snapshot_cutoff_utc"]) for row in baseline[engine]), default="")
        new_cutoff = max((str(row["snapshot_cutoff_utc"]) for row in candidate[engine]), default="")
        if new_cutoff < old_cutoff:
            raise ValueError(f"candidate snapshot cutoff regressed for {engine}")


def write_csv_files(rows_by_engine: Mapping[str, Sequence[Mapping[str, object]]], output_dir: Path) -> list[Path]:
    """Create exactly four CSV files without overwriting existing data."""
    output_dir.mkdir(parents=True, exist_ok=False)
    written: list[Path] = []
    try:
        for engine, filename in OUTPUT_FILES.items():
            path = output_dir / filename
            temporary_path = output_dir / f".{filename}.tmp"
            with temporary_path.open("x", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, lineterminator="\n")
                writer.writeheader()
                for row in rows_by_engine[engine]:
                    writer.writerow({column: row[column] for column in CSV_COLUMNS})
            temporary_path.replace(path)
            written.append(path)
    except KeyboardInterrupt:
        _remove_partial_output(output_dir)
        _thread.interrupt_main()
        raise
    except BaseException:
        _remove_partial_output(output_dir)
        raise
    return written


def _remove_partial_output(output_dir: Path) -> None:
    for filename in OUTPUT_FILES.values():
        (output_dir / filename).unlink(missing_ok=True)
        (output_dir / f".{filename}.tmp").unlink(missing_ok=True)
    try:
        output_dir.rmdir()
    except OSError:
        pass


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a UTC offset")
    return parsed.astimezone(UTC)


def main() -> int:
    parser = argparse.ArgumentParser(description="Export read-only weekly GO Vote aggregates")
    parser.add_argument("--start", type=_parse_utc, default=_parse_utc("2026-04-01T00:00:00Z"))
    parser.add_argument("--snapshot-cutoff", type=_parse_utc)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--baseline-dir", type=Path)
    args = parser.parse_args()

    cutoff = args.snapshot_cutoff or datetime.now(UTC).replace(microsecond=0)
    observations = read_observations(args.start, cutoff)
    rows = build_weekly_rows(observations, args.start, cutoff)
    paths = write_csv_files(rows, args.output_dir)
    baseline = read_csv_files(args.baseline_dir) if args.baseline_dir else None
    validate_publishable(rows, baseline)
    print(f"exported {len(observations)} distinct homepage captures to {len(paths)} CSV files")
    return 0


def validation_main() -> int:
    parser = argparse.ArgumentParser(description="Validate GO Vote aggregate CSV publication")
    parser.add_argument("--candidate-dir", required=True, type=Path)
    parser.add_argument("--baseline-dir", type=Path)
    args = parser.parse_args()
    candidate = read_csv_files(args.candidate_dir)
    baseline = read_csv_files(args.baseline_dir) if args.baseline_dir else None
    validate_publishable(candidate, baseline)
    print("aggregate CSV validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
