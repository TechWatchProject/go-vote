from pathlib import Path

from go_vote.export import OUTPUT_FILES, read_csv_files, validate_publishable

ROOT = Path(__file__).resolve().parents[1]
FROZEN_REPORT = ROOT / "reports" / "2026-04-01_to_2026-08-06T051200Z"


def test_chart_page_links_all_downloads_and_both_metrics() -> None:
    html = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    for filename in (
        "go-vote-google-weekly.csv",
        "go-vote-bing-weekly.csv",
        "go-vote-yahoo-weekly.csv",
        "go-vote-all-engines-weekly.csv",
    ):
        assert f"data/{filename}" in html
    assert "canonical_govote_per_100_homepages" in html
    assert "exact_go_vote_phrase_per_100_homepages" in html
    assert "new Chart" in html
    assert "recorded engine" in html.lower()
    assert 'integrity="sha384-' in html
    assert 'crossorigin="anonymous"' in html


def test_nightly_workflow_is_read_only_and_publishes_docs() -> None:
    workflow = (ROOT / ".github" / "workflows" / "nightly.yml").read_text(encoding="utf-8")
    assert "schedule:" in workflow
    assert "KUMQUAT_READONLY_DSN" in workflow
    assert "go-vote-export" in workflow
    assert "docs/data" in workflow
    assert "contents: write" in workflow
    assert "workflow_dispatch:" in workflow
    assert "jobs:\n  export:" in workflow
    assert "\n  publish:" in workflow
    assert "needs: export" in workflow
    assert "@v" not in workflow


def test_downloadable_csvs_match_validated_frozen_baseline() -> None:
    published = read_csv_files(ROOT / "docs" / "data")
    validate_publishable(published)
    combined = published["All"]
    assert len(combined) == 19
    assert sum(int(str(row["homepage_captures"])) for row in combined) == 11_622
    assert sum(int(str(row["ocr_completed"])) for row in combined) == 11_337
    assert sum(int(str(row["canonical_govote_positive"])) for row in combined) == 242
    assert sum(int(str(row["exact_go_vote_phrase_positive"])) for row in combined) == 0
    for filename in OUTPUT_FILES.values():
        assert (ROOT / "docs" / "data" / filename).read_bytes() == (FROZEN_REPORT / filename).read_bytes()
