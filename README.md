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

## Restored HTML voting series

The report independently joins the established `google_govote_html` table, deduplicating by capture. Render's `job-go-vote-html` resumes the existing HTML classifier, with newest months first and homepage-only input. Its unchanged classifier matches whole-word vote/election/poll in raw captured HTML, including scripts and metadata. This is an HTML keyword series, not confirmed visual reminders. HTML coverage is independent of screenshot OCR coverage. Failed object downloads remain unclassified and retryable.

The main page defaults to HTML, exposes classification coverage, and draws gaps for unclassified weeks. Original OCR measures and publication-regression safeguards remain available. Older CSVs remain readable with zero HTML coverage.
