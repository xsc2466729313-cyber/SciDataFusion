import json
from pathlib import Path

import pytest

from scidatafusion.cli import build_doctor_report, main
from scidatafusion.config import Settings


def test_doctor_report_creates_data_directory(tmp_path: Path) -> None:
    data_dir = tmp_path / "runtime"
    settings = Settings(_env_file=None, data_dir=data_dir)

    report = build_doctor_report(settings)

    assert report["status"] == "ok"
    assert report["data_dir_exists"] is True
    assert data_dir.is_dir()


def test_doctor_command_prints_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SCIDATA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCIDATA_OFFLINE_MODE", "true")

    exit_code = main(["doctor"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert json.loads(captured.out)["status"] == "ok"


def test_doctor_command_reports_invalid_configuration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SCIDATA_OFFLINE_MODE", "false")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("SCIDATA_DASHSCOPE_API_KEY", raising=False)

    exit_code = main(["doctor"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert json.loads(captured.err)["error"] == "invalid_configuration"


def test_configuration_error_does_not_echo_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel = "do-not-print-this-key"
    monkeypatch.setenv("SCIDATA_OFFLINE_MODE", "false")
    monkeypatch.setenv("DASHSCOPE_API_KEY", sentinel)
    monkeypatch.setenv("SCIDATA_QWEN_BASE_URL", "https://example.com/v1")

    exit_code = main(["doctor"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert sentinel not in captured.err


def test_phase1_demo_prints_confirmed_safe_summary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    goal = "Integrate multi-source Type Ia supernova light curves into CSV."
    reviewer = "private-reviewer@example.org"

    exit_code = main(
        [
            "phase1-demo",
            "--goal",
            goal,
            "--confirmed-by",
            reviewer,
        ]
    )
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 0
    assert report["status"] == "confirmed"
    assert report["simulated_capabilities"] is True
    assert report["routing"]["primary_domain"] == "astronomy"
    assert set(report["routing"]["task_packs"]) == {"data_integration", "light_curve"}
    assert report["contract"]["status"] == "confirmed"
    assert report["contract"]["output_formats"] == ["csv"]
    assert goal not in captured.out
    assert reviewer not in captured.out


def test_phase1_demo_review_exit_and_validation_error_are_structured(
    capsys: pytest.CaptureFixture[str],
) -> None:
    review_exit = main(["phase1-demo", "--goal", "data"])
    review_output = json.loads(capsys.readouterr().out)

    assert review_exit == 3
    assert review_output["status"] == "needs_review"
    assert review_output["issues"][0]["blocking"] is True

    invalid_exit = main(["phase1-demo", "--goal", ""])
    invalid_output = json.loads(capsys.readouterr().err)

    assert invalid_exit == 2
    assert invalid_output == {"status": "error", "error": "validation_failed"}


def test_phase2_plan_demo_is_multisource_safe_and_offline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    goal = "Study Type Ia supernova light curves using multi-source data integration into CSV."
    reviewer = "private-phase2-reviewer@example.org"

    exit_code = main(
        [
            "phase2-plan-demo",
            "--goal",
            goal,
            "--confirmed-by",
            reviewer,
        ]
    )
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 0
    assert report["status"] == "succeeded"
    assert report["simulated_capabilities"] is True
    assert report["event_type"] == "search.plan.created"
    assert {item["category"] for item in report["families"]} == {
        "literature_metadata",
        "data_repository",
        "domain_database",
        "supplement_web",
    }
    vizier = next(item for item in report["families"] if item["source_id"] == "vizier_tap")
    assert vizier["dialects"] == ["tap_adql_discovery"]
    assert report["coverage"]["observed_candidates"] == 0
    assert goal not in captured.out
    assert reviewer not in captured.out


def test_phase2_connector_demo_executes_packaged_fixture_without_network(
    capsys: pytest.CaptureFixture[str],
) -> None:
    goal = "Study Type Ia supernova light curves using multi-source data integration into CSV."
    reviewer = "private-m05-reviewer@example.org"

    exit_code = main(
        [
            "phase2-connect-demo",
            "--goal",
            goal,
            "--confirmed-by",
            reviewer,
        ]
    )
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 0
    assert report["status"] == "succeeded"
    assert report["execution_mode"] == "offline_fixture"
    assert report["network_performed"] is False
    assert report["network_status"] == "not_performed"
    assert report["event_type"] == "connector.batch.completed"
    assert report["metrics"] == {
        "query_run_count": 8,
        "successful_query_count": 8,
        "failed_query_count": 0,
        "skipped_query_count": 0,
        "page_count": 9,
        "raw_hit_count": 8,
        "candidate_count": 5,
        "duplicate_hit_count": 3,
        "evidence_count": 8,
        "retry_count": 0,
        "cache_hit_count": 0,
        "live_network_attempt_count": 0,
        "unknown_network_attempt_count": 0,
    }
    assert report["assessment"]["source_category_count"] == 4
    assert goal not in captured.out
    assert reviewer not in captured.out
    assert "evil.example" not in captured.out


def test_phase2_selection_demo_reports_candidate_coverage_and_gaps_safely(
    capsys: pytest.CaptureFixture[str],
) -> None:
    goal = "Study Type Ia supernova light curves using multi-source data integration into CSV."
    reviewer = "private-m06-reviewer@example.org"

    exit_code = main(
        [
            "phase2-select-demo",
            "--goal",
            goal,
            "--confirmed-by",
            reviewer,
        ]
    )
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 0
    assert report["status"] == "partial"
    assert report["execution_mode"] == "offline_fixture"
    assert report["network_performed"] is False
    assert report["candidate_only"] is True
    assert report["metrics"]["candidate_count"] == 5
    assert report["metrics"]["selected_source_count"] == 3
    assert report["coverage"]["required_fields"] == 1.0
    assert report["coverage"]["entity_keys"] == 1.0
    assert report["coverage"]["contract_source_types"] == 1.0
    assert report["coverage"]["selected_source_categories"] == 3
    assert report["coverage"]["has_primary_source"] is True
    assert {item["assigned_category"] for item in report["selected_sources"]} == {
        "literature_metadata",
        "data_repository",
        "domain_database",
    }
    assert {item["code"] for item in report["gaps"]} == {
        "scope_unverified",
        "license_review_required",
    }
    assert report["stop"] == {
        "should_stop": False,
        "reason": "continue_search",
        "outcome": "continue",
        "completed_rounds": 1,
        "recent_marginal_gains": [1.0],
    }
    assert report["event_type"] == "selection.completed"
    assert goal not in captured.out
    assert reviewer not in captured.out
    assert "evil.example" not in captured.out
    assert "https://" not in captured.out


def test_phase3_download_demo_builds_safe_replayable_bronze_summary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    goal = "Study Type Ia supernova light curves using multi-source data integration into CSV."
    reviewer = "private-m07-reviewer@example.org"

    exit_code = main(
        [
            "phase3-download-demo",
            "--goal",
            goal,
            "--confirmed-by",
            reviewer,
        ]
    )
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 0
    assert report["status"] == "partial"
    assert report["execution_mode"] == "offline_fixture"
    assert report["network_performed"] is False
    assert report["network_status"] == "not_performed"
    assert report["event_type"] == "artifact.download.completed"
    assert report["stored_event_count"] == 5
    assert report["event_count"] == 6
    assert report["metrics"] == {
        "selected_source_count": 3,
        "attempted_download_count": 5,
        "stored_download_count": 3,
        "deduplicated_download_count": 1,
        "skipped_download_count": 1,
        "failed_download_count": 0,
        "quarantined_download_count": 0,
        "cache_hit_count": 0,
        "review_required_object_count": 0,
        "acquisition_count": 6,
        "archive_member_count": 2,
        "bronze_object_count": 5,
        "received_bytes": 590,
        "persisted_unique_bytes": 612,
    }
    assert report["relationships"] == {
        "archive_member": 2,
        "landing_attachment": 1,
        "root_download": 3,
    }
    assert set(report["detected_media_types"]) == {
        "application/pdf",
        "application/zip",
        "text/csv",
        "text/html",
        "text/plain",
    }
    assert len(report["objects"]) == 5
    assert all(item["immutable"] is True for item in report["objects"])
    assert all(len(item["byte_sha256"]) == 64 for item in report["objects"])
    assert goal not in captured.out
    assert reviewer not in captured.out
    assert "evil.example" not in captured.out
    assert "malware.exe" not in captured.out
    assert "offline-fixture:" not in captured.out
    assert "https://" not in captured.out


def test_phase3_parse_plan_demo_routes_every_object_without_parsing_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    goal = "Study Type Ia supernova light curves using multi-source data integration into CSV."
    reviewer = "private-m08-reviewer@example.org"

    exit_code = main(
        [
            "phase3-parse-plan-demo",
            "--goal",
            goal,
            "--confirmed-by",
            reviewer,
        ]
    )
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 0
    assert report["status"] == "succeeded"
    assert report["execution_mode"] == "offline"
    assert report["network_performed"] is False
    assert report["model_classification_performed"] is False
    assert report["downstream_parser_executions"] == 0
    assert report["bronze_writes"] == 0
    assert report["metrics"] == {
        "artifact_count": 5,
        "classification_count": 5,
        "route_count": 5,
        "page_route_count": 0,
        "succeeded_plan_count": 5,
        "partial_plan_count": 0,
        "review_plan_count": 0,
        "unsupported_plan_count": 0,
        "failed_plan_count": 0,
        "gap_count": 0,
        "format_gap_count": 0,
        "capability_gap_count": 0,
        "model_candidate_classification_count": 0,
        "high_resource_primary_route_count": 0,
        "planned_cost_micro_usd": 5000,
    }
    assert report["format_families"] == {
        "archive": 1,
        "csv": 1,
        "html": 1,
        "pdf": 1,
        "plain_text": 1,
    }
    assert report["route_dispositions"] == {"metadata_only": 1, "parse": 4}
    assert report["target_modules"] == {"M09": 3, "M10": 1}
    assert report["primary_parsers"] == {
        "m09.html": 1,
        "m09.pdf_text": 1,
        "m09.text": 1,
        "m10.csv": 1,
    }
    assert report["fallback_count"] == 1
    assert report["event_type"] == "parse.plan.created"
    assert report["event_count"] == 1
    for secret_or_content in (
        goal,
        reviewer,
        "evil.example",
        "malware.exe",
        "offline-fixture:",
        "https://",
        "photometry.csv",
        "59000.1",
        "12.3",
    ):
        assert secret_or_content not in captured.out


def test_phase3_document_demo_produces_safe_partial_ir_summary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    goal = "Study Type Ia supernova light curves using multi-source data integration into CSV."
    reviewer = "private-m09-reviewer@example.org"

    exit_code = main(
        [
            "phase3-document-demo",
            "--goal",
            goal,
            "--confirmed-by",
            reviewer,
        ]
    )
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 0
    assert report["status"] == "partial"
    assert report["execution_mode"] == "offline"
    assert report["network_performed"] is False
    assert report["model_performed"] is False
    assert report["bronze_writes"] == 0
    assert report["m10_table_executions"] == 0
    assert report["m11_chart_executions"] == 0
    assert report["m13_field_extractions"] == 0
    assert report["metrics"] == {
        "eligible_route_count": 3,
        "succeeded_route_count": 2,
        "partial_route_count": 0,
        "review_route_count": 1,
        "unsupported_route_count": 0,
        "failed_route_count": 0,
        "attempt_count": 4,
        "fallback_attempt_count": 1,
        "candidate_count": 2,
        "document_ir_count": 2,
        "page_count": 2,
        "block_count": 2,
        "text_character_count": 37,
        "gap_count": 2,
        "model_attempt_count": 0,
        "network_attempt_count": 0,
        "actual_cost_micro_usd": 0,
    }
    assert report["route_statuses"] == {"needs_review": 1, "succeeded": 2}
    assert report["attempt_statuses"] == {"blocked": 1, "failed": 1, "succeeded": 2}
    assert report["parser_attempts"] == {
        "m09.html": 1,
        "m09.pdf_ocr": 1,
        "m09.pdf_text": 1,
        "m09.text": 1,
    }
    assert report["blocked_parsers"] == ["m09.pdf_ocr"]
    assert report["quality_checks"] == {
        "output_schema": {"failed": 0, "passed": 2},
        "reading_order": {"failed": 0, "passed": 1},
        "text_coverage": {"failed": 0, "passed": 2},
    }
    assert report["event_type"] == "document.parsed"
    assert report["event_count"] == 1
    for secret_or_content in (
        goal,
        reviewer,
        "evil.example",
        "malware.exe",
        "offline-fixture:",
        "https://",
        "Paper PDF",
        "Observed magnitude",
        "59000.1",
        "12.3",
    ):
        assert secret_or_content not in captured.out


def test_phase3_table_demo_produces_safe_cell_evidence_summary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    goal = "Study Type Ia supernova light curves using multi-source data integration into CSV."
    reviewer = "private-m10-reviewer@example.org"

    exit_code = main(["phase3-table-demo", "--goal", goal, "--confirmed-by", reviewer])
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 0
    assert report["status"] == "succeeded"
    assert report["execution_mode"] == "offline"
    assert report["network_performed"] is False
    assert report["model_performed"] is False
    assert report["bronze_writes"] == 0
    assert report["m13_field_extractions"] == 0
    assert report["metrics"] == {
        "eligible_route_count": 1,
        "succeeded_route_count": 1,
        "review_route_count": 0,
        "failed_route_count": 0,
        "attempt_count": 1,
        "table_count": 1,
        "row_count": 2,
        "column_count": 4,
        "cell_count": 8,
        "exact_cell_evidence_count": 8,
        "gap_count": 0,
        "model_attempt_count": 0,
        "network_attempt_count": 0,
        "actual_cost_micro_usd": 0,
    }
    assert report["route_statuses"] == {"succeeded": 1}
    assert report["attempt_statuses"] == {"succeeded": 1}
    assert report["parser_attempts"] == {"m10.csv": 1}
    assert report["quality_checks"] == {
        "cell_evidence": {"failed": 0, "passed": 1},
        "output_schema": {"failed": 0, "passed": 1},
        "table_structure": {"failed": 0, "passed": 1},
    }
    assert report["event_type"] == "table.parsed"
    assert report["event_count"] == 1
    for secret_or_content in (
        goal,
        reviewer,
        "evil.example",
        "malware.exe",
        "offline-fixture:",
        "https://",
        "object_id",
        "observation_time",
        "SN-A",
        "59000.1",
        "12.3",
    ):
        assert secret_or_content not in captured.out


def test_phase4_extract_demo_produces_safe_evidence_coverage_summary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    goal = "Study Type Ia supernova light curves using multi-source data integration into CSV."
    reviewer = "private-m13-reviewer@example.org"

    exit_code = main(["phase4-extract-demo", "--goal", goal, "--confirmed-by", reviewer])
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 0
    assert report["status"] == "partial"
    assert report["execution_mode"] == "offline"
    assert report["network_performed"] is False
    assert report["model_performed"] is False
    assert report["gold_writes"] == 0
    assert report["m14_mapping_executions"] == 0
    assert report["metrics"] == {
        "input_table_count": 1,
        "accepted_table_count": 1,
        "input_data_row_count": 1,
        "extracted_row_count": 1,
        "evidence_atom_count": 4,
        "candidate_count": 4,
        "explicit_candidate_count": 4,
        "inferred_candidate_count": 0,
        "derived_candidate_count": 0,
        "evidence_coverage": 1.0,
        "required_field_coverage": 0.75,
        "entity_bound_candidate_count": 4,
        "gap_count": 1,
        "model_attempt_count": 0,
        "network_attempt_count": 0,
        "actual_cost_micro_usd": 0,
    }
    assert report["candidate_fields"] == {
        "band": 1,
        "magnitude": 1,
        "object_id": 1,
        "observation_time": 1,
    }
    assert report["candidate_origins"] == {"explicit": 4}
    assert report["evidence_source_kinds"] == {"table_cell": 4}
    assert report["gap_codes"] == {"required_field_header_missing": 1}
    assert report["event_type"] == "field.extracted"
    assert report["event_count"] == 1
    for secret_or_content in (
        goal,
        reviewer,
        "evil.example",
        "malware.exe",
        "offline-fixture:",
        "https://",
        "SN-A",
        "59000.1",
        "12.3",
        "deterministic_exact_header_table_cell",
    ):
        assert secret_or_content not in captured.out


def test_phase4_map_demo_produces_safe_threshold_and_evidence_summary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    goal = "Study Type Ia supernova light curves using multi-source data integration into CSV."
    reviewer = "private-m14-reviewer@example.org"

    exit_code = main(["phase4-map-demo", "--goal", goal, "--confirmed-by", reviewer])
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 0
    assert report["status"] == "partial"
    assert report["execution_mode"] == "offline"
    assert report["network_performed"] is False
    assert report["model_performed"] is False
    assert report["embedding_performed"] is False
    assert report["gold_writes"] == 0
    assert report["m15_normalization_executions"] == 0
    assert report["metrics"] == {
        "input_candidate_count": 4,
        "mapping_count": 4,
        "auto_accepted_count": 4,
        "blocked_mapping_count": 0,
        "unmapped_field_count": 0,
        "alias_suggestion_count": 0,
        "upstream_gap_count": 1,
        "mapping_evidence_count": 4,
        "evidence_coverage": 1.0,
        "automatic_acceptance_rate": 1.0,
        "m15_eligible_count": 4,
        "model_attempt_count": 0,
        "embedding_attempt_count": 0,
        "network_attempt_count": 0,
        "actual_cost_micro_usd": 0,
    }
    assert report["mapping_methods"] == {"exact_contract_field": 4}
    assert report["mapping_decisions"] == {"auto_accepted": 4}
    assert report["eligible_target_fields"] == {
        "band": 1,
        "magnitude": 1,
        "object_id": 1,
        "observation_time": 1,
    }
    assert report["unmapped_reasons"] == {}
    assert report["event_type"] == "field.mapped"
    assert report["event_count"] == 1
    for secret_or_content in (
        goal,
        reviewer,
        "evil.example",
        "malware.exe",
        "offline-fixture:",
        "https://",
        "SN-A",
        "59000.1",
        "12.3",
        "mjd",
        "filter",
    ):
        assert secret_or_content not in captured.out


def test_phase4_normalize_demo_reports_traceability_without_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    goal = "Study Type Ia supernova light curves using multi-source data integration into CSV."
    reviewer = "private-m15-reviewer@example.org"

    exit_code = main(["phase4-normalize-demo", "--goal", goal, "--confirmed-by", reviewer])
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 0
    assert report["status"] == "partial"
    assert report["execution_mode"] == "offline"
    assert report["network_performed"] is False
    assert report["model_performed"] is False
    assert report["llm_value_mutations"] == 0
    assert report["gold_writes"] == 0
    assert report["metrics"]["normalized_field_count"] == 4
    assert report["metrics"]["transformation_count"] == 2
    assert report["metrics"]["issue_count"] == 3
    assert report["metrics"]["m16_eligible_field_count"] == 2
    assert report["field_statuses"] == {"needs_review": 2, "normalized": 2}
    assert report["value_kinds"] == {"decimal": 2, "string": 2}
    assert report["transformation_kinds"] == {"parse_decimal_exact": 2}
    assert report["issue_codes"] == {"source_unit_missing": 2, "time_scale_missing": 1}
    assert report["event_type"] == "record.normalized"
    for private_content in (goal, reviewer, "SN-A", "59000.1", "12.3", "MJD", "mag"):
        assert private_content not in captured.out


def test_phase5_resolve_demo_reports_singleton_without_entity_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    goal = "Study Type Ia supernova light curves using multi-source data integration into CSV."
    reviewer = "private-m16-reviewer@example.org"

    exit_code = main(["phase5-resolve-demo", "--goal", goal, "--confirmed-by", reviewer])
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 0
    assert report["status"] == "partial"
    assert report["execution_mode"] == "offline"
    assert report["network_performed"] is False
    assert report["model_performed"] is False
    assert report["fuzzy_auto_merges"] == 0
    assert report["llm_merge_decisions"] == 0
    assert report["gold_writes"] == 0
    assert report["metrics"]["input_record_count"] == 1
    assert report["metrics"]["candidate_pair_count"] == 0
    assert report["metrics"]["entity_cluster_count"] == 1
    assert report["metrics"]["singleton_cluster_count"] == 1
    assert report["metrics"]["automatic_merge_cluster_count"] == 0
    assert report["metrics"]["duplicate_group_count"] == 0
    assert report["resolution_methods"] == {"exact_stable_identifier": 1}
    assert report["cluster_decisions"] == {"singleton": 1}
    assert report["duplicate_methods"] == {}
    assert report["event_type"] == "entity.resolved"
    for private_content in (goal, reviewer, "SN-A", "59000.1", "12.3", "object_id"):
        assert private_content not in captured.out


def test_phase5_fuse_demo_reports_decisions_without_scientific_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    goal = "Study Type Ia supernova light curves using multi-source data integration into CSV."
    reviewer = "private-m17-reviewer@example.org"

    exit_code = main(["phase5-fuse-demo", "--goal", goal, "--confirmed-by", reviewer])
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 0
    assert report["status"] == "partial"
    assert report["execution_mode"] == "offline"
    assert report["network_performed"] is False
    assert report["model_performed"] is False
    assert report["tolerance_aggregations"] == 0
    assert report["source_priority_selections"] == 0
    assert report["llm_value_decisions"] == 0
    assert report["silent_overwrites"] == 0
    assert report["final_gold_writes"] == 0
    assert report["metrics"]["candidate_count"] == 4
    assert report["metrics"]["selected_field_count"] == 2
    assert report["metrics"]["withheld_field_count"] == 2
    assert report["metrics"]["conflict_count"] == 0
    assert report["metrics"]["gold_evidence_coverage"] == 1.0
    assert report["fusion_decisions"] == {"single_eligible": 2, "withheld_review": 2}
    assert report["conflict_classes"] == {}
    assert report["event_type"] == "fusion.completed"
    for private_content in (
        goal,
        reviewer,
        "SN-A",
        "59000.1",
        "12.3",
        "object_id",
        "observation_time",
        "magnitude",
        "band",
    ):
        assert private_content not in captured.out


def test_phase5_audit_demo_reports_review_queue_without_scientific_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    goal = "Study Type Ia supernova light curves using multi-source data integration into CSV."
    reviewer = "private-m18-reviewer@example.org"

    exit_code = main(["phase5-audit-demo", "--goal", goal, "--confirmed-by", reviewer])
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 3
    assert report["status"] == "needs_review"
    assert report["execution_mode"] == "offline"
    assert report["network_performed"] is False
    assert report["model_performed"] is False
    assert report["automatic_repairs_executed"] == 0
    assert report["scientific_value_mutations"] == 0
    assert report["formal_gold_published"] is False
    assert report["quality_gate_passed"] is False
    assert report["metrics"]["gate_count"] == 3
    assert report["metrics"]["issue_count"] == 3
    assert report["metrics"]["review_queue_count"] == 3
    assert report["metrics"]["formal_gold_record_count"] == 0
    assert report["gate_kinds"] == {
        "any_of_fields": 1,
        "field_provenance": 1,
        "required_fields": 1,
    }
    assert report["issue_codes"] == {
        "any_of_fields_missing": 1,
        "field_provenance_missing": 1,
        "required_field_missing": 1,
    }
    assert report["issue_severities"] == {"critical": 3}
    assert report["planned_actions"] == {"request_human": 3}
    assert report["review_statuses"] == {"pending": 3}
    assert report["event_type"] == "quality.gated"
    for private_content in (
        goal,
        reviewer,
        "SN-A",
        "59000.1",
        "12.3",
        "object_id",
        "source_record_id",
        "observation_time",
        "magnitude",
        "band",
    ):
        assert private_content not in captured.out
