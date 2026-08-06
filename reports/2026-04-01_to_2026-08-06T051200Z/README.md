# GO Vote weekly aggregate report

This immutable report covers homepage captures recorded from `2026-04-01T00:00:00Z` (inclusive) through `2026-08-06T05:12:00Z` (exclusive). Weeks begin Monday at 00:00 UTC. The first and last rows are marked as partial weeks because the report boundaries do not coincide with full ISO weeks.

## Files and reconciliation

| File | Weeks | Homepage captures | OCR completed | Canonical positives | Exact phrase positives |
|---|---:|---:|---:|---:|---:|
| `go-vote-google-weekly.csv` | 19 | 6,179 | 5,993 | 66 | 0 |
| `go-vote-bing-weekly.csv` | 19 | 2,591 | 2,544 | 81 | 0 |
| `go-vote-yahoo-weekly.csv` | 19 | 2,852 | 2,800 | 95 | 0 |
| `go-vote-all-engines-weekly.csv` | 19 | 11,622 | 11,337 | 242 | 0 |

For every week and every count column, the combined file equals Google + Bing + Yahoo. Independent aggregate SQL returned the same totals and found zero capture IDs with duplicate OCR rows.

## Metric definitions

- `canonical_govote_positive` is the existing broad historical classifier, equivalent to the case-insensitive whole-word concepts `vote`, `election`, or `poll`. It is not an exact “go vote” match.
- `exact_go_vote_phrase_positive` requires the whole phrase `go vote`, case-insensitively, with one or more whitespace characters between the words. No completed OCR in this frozen range matched that literal phrase.
- `*_per_100_homepages` divides by all captured homepages, so unrecoverable OCR remains visible in the denominator.
- `*_per_100_ocred` divides by completed OCR only.
- `recorded_engine` is the engine stored with the capture. Yahoo combines `search.yahoo.com` and `www.yahoo.com`.
- `classifier_version` is `canonical-vote-election-poll-v1+exact-go-vote-v1`.

## OCR recovery ceiling

The recovery reached 100% of the recoverable frozen cohort. Raw OCR coverage is 11,337 / 11,622 (97.55%). The remaining 285 gaps are immutable exclusions:

- 275 May screenshots were unavailable from both frozen source observations: Google 186, Bing 37, Yahoo 52.
- 10 reviewed screenshots exceeded the OCR service's 10 MiB limit: seven in April and three in May, all recorded as Bing.

There are no unclassified gaps. Recovery used immutable per-month manifests, max concurrency 4 per bounded Render job, server-enforced no-overwrite copy behavior, and dual-backend verification. No source object was removed.

## Recorded-engine caveat

Issue `TechWatchProject/twp#175` documents screenshots that visually appeared to be Yahoo but were stored with Bing metadata. This report performs no visual reclassification and therefore labels the dimension `recorded_engine`.

## Reproduction

The exporter uses the server-enforced `sentiment_readonly` account, a pinned production database identity and project CA, a read-only consistent transaction, server-side phrase classification, and client-side weekly aggregation of distinct capture IDs. It never exports OCR text, UIDs, capture IDs, or screenshot keys.

```console
uv run go-vote-export \
  --start 2026-04-01T00:00:00Z \
  --snapshot-cutoff 2026-08-06T05:12:00Z \
  --output-dir generated-report
uv run go-vote-validate \
  --candidate-dir generated-report \
  --baseline-dir reports/2026-04-01_to_2026-08-06T051200Z
```

Verify the files with `SHA256SUMS` before charting.
