from pathlib import Path

from go_vote.export import read_csv_files, validate_publishable

ROOT = Path(__file__).resolve().parents[1]
FROZEN_REPORT = ROOT / "reports" / "2026-04-01_to_2026-08-06T051200Z"


def test_nothing_is_published_to_github_pages() -> None:
    """This repo is public. The report lives behind GitHub sign-in on nexus."""
    assert not (ROOT / "docs").exists()


def test_nightly_workflow_is_read_only_and_publishes_to_nexus() -> None:
    workflow = (ROOT / ".github" / "workflows" / "nightly.yml").read_text(encoding="utf-8")
    assert "schedule:" in workflow
    assert "KUMQUAT_READONLY_DSN" in workflow
    assert "go-vote-export" in workflow
    assert "go-vote-publish" in workflow
    assert "workflow_dispatch:" in workflow
    assert "jobs:\n  export:" in workflow
    assert "\n  publish:" in workflow
    assert "needs: export" in workflow
    assert "@v" not in workflow
    for banned in ("contents: write", "git push", "docs/", "pages"):
        assert banned not in workflow
    export_job, publish_job = workflow.split("\n  publish:")
    assert "GO_VOTE_INGEST_TOKEN" not in export_job
    assert "KUMQUAT_READONLY_DSN" not in publish_job


def test_frozen_report_retains_validated_baseline_counts() -> None:
    frozen = read_csv_files(FROZEN_REPORT)
    validate_publishable(frozen)
    combined = frozen["All"]
    assert len(combined) == 19
    assert sum(int(str(row["homepage_captures"])) for row in combined) == 11_622
    assert sum(int(str(row["ocr_completed"])) for row in combined) == 11_337
    assert sum(int(str(row["canonical_govote_positive"])) for row in combined) == 242
    assert sum(int(str(row["exact_go_vote_phrase_positive"])) for row in combined) == 0
