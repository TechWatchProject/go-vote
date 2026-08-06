# GO Vote weekly data

This repository publishes Tech Watch Project's weekly GO Vote aggregate data and a Chart.js visualization. The public output starts at April 1, 2026, separates Google, Bing, and Yahoo according to the engine recorded with each capture, and includes a reconciled all-engine series.

The exporter is deliberately read-only and privacy-limited:

- it refuses any database username except `sentiment_readonly`;
- it pins the production host, port, database, and private project CA, rejects DSN options, verifies negotiated TLS, and accepts only the account's known read-only grants;
- it selects only fields needed for aggregate classification and never writes to the database;
- it collapses duplicate OCR rows to distinct homepage captures;
- it writes only weekly aggregate CSVs, never OCR text or capture IDs; and
- it fails on unknown engine labels or failed all-engine reconciliation.

## Local development

```console
uv sync --extra dev
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
uv run pytest
```

To export against an authorized read-only database connection:

```console
$env:KUMQUAT_READONLY_DSN = "mysql+pymysql://sentiment_readonly:..."
uv run go-vote-export --output-dir generated-data
```

The output directory must not already exist. The default interval is April 1, 2026 through the current UTC time; `--snapshot-cutoff` can freeze an immutable report.

## Publishing

The nightly workflow reads `KUMQUAT_READONLY_DSN` from a GitHub Actions secret in an export-only job. A separate publishing job has repository write permission but never receives the database secret. Before publication, the candidate must reconcile and must not drop a week or reduce any capture, OCR, or positive count from the checked-in baseline. GitHub Pages serves `docs/` from `main`.
