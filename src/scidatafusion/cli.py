"""Local operational commands for the engineering baseline."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from scidatafusion import __version__
from scidatafusion.artifacts import (
    ArtifactDownloadService,
    BronzeByteStore,
    MemoryBronzeStore,
    build_offline_ia_artifact_bundle,
)
from scidatafusion.config import Settings
from scidatafusion.connectors.executor import ConnectorBatchExecutor
from scidatafusion.connectors.fixtures import build_offline_ia_connector_bundle
from scidatafusion.connectors.registry import load_default_connector_registry
from scidatafusion.contracts.artifacts import (
    ArtifactDownloadRequest,
    ArtifactDownloadResult,
    ArtifactDownloadStatus,
)
from scidatafusion.contracts.base import utc_now
from scidatafusion.contracts.connectors import (
    ConnectorBatchStatus,
    ConnectorExecutionRequest,
    ConnectorExecutionResult,
)
from scidatafusion.contracts.documents import (
    DocumentParsingRequest,
    DocumentParsingResult,
    DocumentParsingStatus,
)
from scidatafusion.contracts.extraction import (
    ExtractionRequest,
    ExtractionResult,
    ExtractionStatus,
)
from scidatafusion.contracts.mapping import MappingRequest, MappingResult, MappingStatus
from scidatafusion.contracts.normalization import (
    NormalizationRequest,
    NormalizationResult,
    NormalizationStatus,
)
from scidatafusion.contracts.parsing import (
    ParsePlanningRequest,
    ParsePlanningResult,
    ParsePlanningStatus,
)
from scidatafusion.contracts.scientific import ScientificDataContract
from scidatafusion.contracts.search import (
    SearchCapabilityMode,
    SearchPlanningRequest,
    SearchPlanningResult,
)
from scidatafusion.contracts.selection import (
    SourceSelectionRequest,
    SourceSelectionResult,
    SourceSelectionStatus,
)
from scidatafusion.contracts.tables import (
    TableParsingRequest,
    TableParsingResult,
    TableParsingStatus,
)
from scidatafusion.contracts.task import TaskIntakeRequest
from scidatafusion.contracts.workflow import Phase1Status, Phase1WorkflowResult
from scidatafusion.documents.fixtures import build_offline_document_parsing_bundle
from scidatafusion.documents.service import DocumentParsingService
from scidatafusion.domain.registry import RegistryLoadError
from scidatafusion.errors import AppError
from scidatafusion.extraction.fixtures import build_offline_extraction_bundle
from scidatafusion.extraction.service import EvidenceFirstExtractionService
from scidatafusion.mapping.fixtures import build_offline_mapping_bundle
from scidatafusion.mapping.service import FieldMappingService
from scidatafusion.normalization.fixtures import build_offline_normalization_bundle
from scidatafusion.normalization.service import ScientificNormalizationService
from scidatafusion.parsing import ParsePlanningService
from scidatafusion.parsing.fixtures import build_offline_parse_planning_bundle
from scidatafusion.search import SearchPlanner, SourceCapabilityRegistryLoader, source_ids
from scidatafusion.selection import SourceSelectionService
from scidatafusion.tables.fixtures import build_offline_table_parsing_bundle
from scidatafusion.tables.service import TableParsingService
from scidatafusion.workflow import build_offline_demo_workflow


def build_doctor_report(settings: Settings) -> dict[str, object]:
    """Check local runtime prerequisites without making network requests."""

    data_dir = Path(settings.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    return {
        "status": "ok",
        "package": "scidatafusion",
        "version": __version__,
        "python": ".".join(str(part) for part in sys.version_info[:3]),
        "data_dir_exists": data_dir.is_dir(),
        "settings": settings.diagnostic_summary(),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scidatafusion")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="validate local configuration without network calls")
    phase1 = subparsers.add_parser(
        "phase1-demo",
        help="run the offline M00-M03 workflow with explicitly simulated capabilities",
    )
    phase1.add_argument("--goal", required=True, help="scientific research goal")
    phase1.add_argument(
        "--confirmed-by",
        help="explicit reviewer identity; omitted leaves the contract as a draft",
    )
    phase2 = subparsers.add_parser(
        "phase2-plan-demo",
        help="run offline Phase 1 and M04 with explicitly simulated source capabilities",
    )
    phase2.add_argument("--goal", required=True, help="scientific research goal")
    phase2.add_argument(
        "--confirmed-by",
        required=True,
        help="explicit reviewer identity required by the M04 contract gate",
    )
    connectors = subparsers.add_parser(
        "phase2-connect-demo",
        help="run M00-M05 against packaged Connector responses without network access",
    )
    connectors.add_argument("--goal", required=True, help="scientific research goal")
    connectors.add_argument(
        "--confirmed-by",
        required=True,
        help="explicit reviewer identity required by the M04 contract gate",
    )
    selection = subparsers.add_parser(
        "phase2-select-demo",
        help="run M00-M06 with candidate-only coverage and source selection offline",
    )
    selection.add_argument("--goal", required=True, help="scientific research goal")
    selection.add_argument(
        "--confirmed-by",
        required=True,
        help="explicit reviewer identity required by the M04 contract gate",
    )
    artifacts = subparsers.add_parser(
        "phase3-download-demo",
        help="run M00-M07 against packaged source bytes without external network access",
    )
    artifacts.add_argument("--goal", required=True, help="scientific research goal")
    artifacts.add_argument(
        "--confirmed-by",
        required=True,
        help="explicit reviewer identity required by the M04 contract gate",
    )
    parse_plan = subparsers.add_parser(
        "phase3-parse-plan-demo",
        help="run M00-M08 and plan downstream parsers without executing them",
    )
    parse_plan.add_argument("--goal", required=True, help="scientific research goal")
    parse_plan.add_argument(
        "--confirmed-by",
        required=True,
        help="explicit reviewer identity required by the M04 contract gate",
    )
    documents = subparsers.add_parser(
        "phase3-document-demo",
        help="run M00-M09 and produce document IR with deterministic offline parsers",
    )
    documents.add_argument("--goal", required=True, help="scientific research goal")
    documents.add_argument(
        "--confirmed-by",
        required=True,
        help="explicit reviewer identity required by the M04 contract gate",
    )
    tables = subparsers.add_parser(
        "phase3-table-demo",
        help="run M00-M10 and produce cell-evidenced TableIR from native CSV offline",
    )
    tables.add_argument("--goal", required=True, help="scientific research goal")
    tables.add_argument(
        "--confirmed-by",
        required=True,
        help="explicit reviewer identity required by the M04 contract gate",
    )
    extraction = subparsers.add_parser(
        "phase4-extract-demo",
        help="run M00-M13 and create evidence-bound explicit field candidates offline",
    )
    extraction.add_argument("--goal", required=True, help="scientific research goal")
    extraction.add_argument(
        "--confirmed-by",
        required=True,
        help="explicit reviewer identity required by the M04 contract gate",
    )
    mapping = subparsers.add_parser(
        "phase4-map-demo",
        help="run M00-M14 and validate evidence-backed canonical field mappings offline",
    )
    mapping.add_argument("--goal", required=True, help="scientific research goal")
    mapping.add_argument(
        "--confirmed-by",
        required=True,
        help="explicit reviewer identity required by the M04 contract gate",
    )
    normalization = subparsers.add_parser(
        "phase4-normalize-demo",
        help="run M00-M15 with exact parsing and no scientific-context guessing offline",
    )
    normalization.add_argument("--goal", required=True, help="scientific research goal")
    normalization.add_argument(
        "--confirmed-by",
        required=True,
        help="explicit reviewer identity required by the M04 contract gate",
    )
    return parser


def build_phase1_summary(result: Phase1WorkflowResult) -> dict[str, object]:
    """Render a public demo summary without research text, evidence, URLs, or identities."""

    routing: dict[str, object] | None = None
    if result.routing is not None:
        routing = {
            "status": result.routing.status.value,
            "mode": result.routing.pack_selection.mode.value,
            "primary_domain": result.routing.domain_profile.primary_domain,
            "task_archetypes": list(result.routing.task_archetypes.archetypes),
            "domain_packs": [item.name for item in result.routing.pack_selection.domain_packs],
            "task_packs": [item.name for item in result.routing.pack_selection.task_packs],
            "missing_capabilities": list(result.routing.pack_selection.missing_capabilities),
            "decision_hash": result.routing.decision_hash,
        }
    contract: dict[str, object] | None = None
    if result.compilation is not None:
        artifact = (
            result.confirmation.contract
            if result.confirmation is not None
            else result.compilation.contract
        )
        contract = {
            "status": artifact.status.value,
            "contract_id": artifact.contract_id,
            "contract_hash": artifact.contract_hash,
            "schema_hash": artifact.schema_hash,
            "fields": [field.name for field in artifact.fields],
            "entity_keys": list(artifact.entity_keys),
            "output_formats": list(artifact.output_formats),
            "warning_count": len(result.compilation.warnings),
            "conflict_count": len(result.compilation.conflicts),
        }
    return {
        "status": result.status.value,
        "capability_mode": result.capability_mode.value,
        "simulated_capabilities": result.capability_mode.value == "simulated_demo",
        "task_id": result.task_id,
        "run_id": result.run_id,
        "m00_status": result.intake.status.value,
        "m01_status": result.problem.status.value if result.problem is not None else None,
        "routing": routing,
        "contract": contract,
        "checkpoints": [
            {
                "sequence": item.sequence,
                "module_id": item.module_id,
                "event_type": item.event_type.value,
                "status": item.status,
                "output_hash": item.output_hash,
            }
            for item in result.checkpoints
        ],
        "issues": [
            {"stage": item.stage, "code": item.code, "blocking": item.blocking}
            for item in result.issues
        ],
    }


def build_search_plan_summary(result: SearchPlanningResult) -> dict[str, object]:
    """Render a safe M04 demo summary without query text or reviewer identity."""

    return {
        "status": result.status.value,
        "capability_mode": result.plan.capability_mode.value,
        "simulated_capabilities": (
            result.plan.capability_mode is SearchCapabilityMode.SIMULATED_DEMO
        ),
        "task_id": result.task_id,
        "run_id": result.run_id,
        "plan_id": result.plan.plan_id,
        "plan_hash": result.plan.plan_hash,
        "registry_hash": result.plan.capability_registry_hash,
        "families": [
            {
                "source_id": family.source_id,
                "category": family.category.value,
                "state": family.state.value,
                "query_count": len(family.queries),
                "dialects": sorted({query.dialect.value for query in family.queries}),
                "languages": sorted({query.language for query in family.queries}),
                "target_fields": list(family.target_fields),
            }
            for family in result.plan.query_family_set.families
        ],
        "coverage": {
            "cell_count": len(result.plan.coverage_matrix.cells),
            "planned_cells": sum(
                item.state.value == "planned" for item in result.plan.coverage_matrix.cells
            ),
            "observed_candidates": sum(
                item.observed_candidate_count for item in result.plan.coverage_matrix.cells
            ),
        },
        "budget": {
            "allocated_query_count": result.plan.budget_allocation.allocated_query_count,
            "allocated_cost_micro_usd": (result.plan.budget_allocation.allocated_cost_micro_usd),
            "allocated_duration_seconds": (
                result.plan.budget_allocation.allocated_duration_seconds
            ),
        },
        "gaps": [
            {
                "code": item.code.value,
                "source_id": item.source_id,
                "blocking": item.blocking,
            }
            for item in result.plan.gaps
        ],
        "event_type": result.event.event_type.value,
        "output_hash": result.output_hash,
    }


def build_connector_summary(result: ConnectorExecutionResult) -> dict[str, object]:
    """Render M05 counts and hashes without candidate content or untrusted source text."""

    candidates = result.candidate_set.candidates
    return {
        "status": result.status.value,
        "execution_mode": "offline_fixture",
        "network_performed": (
            None
            if result.metrics.unknown_network_attempt_count
            else result.metrics.live_network_attempt_count > 0
        ),
        "network_status": (
            "unknown"
            if result.metrics.unknown_network_attempt_count
            else "performed"
            if result.metrics.live_network_attempt_count
            else "not_performed"
        ),
        "task_id": result.task_id,
        "run_id": result.run_id,
        "plan_id": result.candidate_set.search_plan_id,
        "plan_hash": result.candidate_set.search_plan_hash,
        "connector_registry_hash": result.candidate_set.connector_registry_hash,
        "metrics": result.metrics.model_dump(mode="json"),
        "assessment": {
            "primary_candidate_count": sum(item.primary_source for item in candidates),
            "source_category_count": len(
                {category for item in candidates for category in item.categories}
            ),
            "coverage_claim_count": sum(len(item.coverage_claims) for item in candidates),
            "metadata_conflict_count": sum(len(item.conflicts) for item in candidates),
        },
        "event_type": result.event.event_type.value,
        "output_hash": result.output_hash,
    }


def build_selection_summary(result: SourceSelectionResult) -> dict[str, object]:
    """Render M06 decisions without source text, URLs, excerpts, or reviewer identity."""

    report = result.coverage_report
    return {
        "status": result.status.value,
        "execution_mode": "offline_fixture",
        "network_performed": False,
        "network_status": "not_performed",
        "candidate_only": report.candidate_only,
        "task_id": result.task_id,
        "run_id": result.run_id,
        "selection_id": result.selected_source_set.selection_id,
        "selected_source_set_hash": result.selected_source_set.selected_source_set_hash,
        "coverage_report_hash": report.coverage_report_hash,
        "search_gap_set_hash": result.search_gap_set.search_gap_set_hash,
        "metrics": result.metrics.model_dump(mode="json"),
        "coverage": {
            "required_fields": report.required_candidate_coverage,
            "entity_keys": report.entity_key_candidate_coverage,
            "contract_source_types": report.source_type_candidate_coverage,
            "selected_source_categories": len(report.selected_categories),
            "has_primary_source": report.has_primary_source,
            "fields": [
                {
                    "name": item.field_name,
                    "requirement": item.requirement.value,
                    "state": item.state.value,
                    "candidate_count": len(item.candidate_ids),
                }
                for item in report.fields
            ],
        },
        "selected_sources": [
            {
                "rank": item.selection_rank,
                "candidate_id": item.candidate_id,
                "assigned_category": item.assigned_diversity_category.value,
                "primary_source": item.primary_source,
                "covered_fields": list(item.covered_fields),
                "contract_source_types": list(item.covered_contract_source_types),
                "download_readiness": item.download_readiness.value,
                "license_decision": item.license_decision.value,
                "reason_codes": [reason.code.value for reason in item.reasons],
                "candidate_only": item.candidate_only,
            }
            for item in result.selected_source_set.sources
        ],
        "gaps": [
            {
                "code": item.code.value,
                "blocking": item.blocking,
                "target_fields": list(item.target_fields),
                "contract_source_types": list(item.contract_source_types),
                "categories": [category.value for category in item.categories],
            }
            for item in result.search_gap_set.gaps
        ],
        "stop": {
            "should_stop": result.stop_decision.should_stop,
            "reason": result.stop_decision.reason.value,
            "outcome": result.stop_decision.outcome.value,
            "completed_rounds": result.progress_snapshot.completed_rounds,
            "recent_marginal_gains": list(result.progress_snapshot.recent_marginal_gains),
        },
        "event_type": result.event.event_type.value,
        "output_hash": result.output_hash,
    }


def build_artifact_summary(result: ArtifactDownloadResult) -> dict[str, object]:
    """Render M07 provenance without URLs, filenames, approvals, or source content."""

    attempts = result.run_log.attempts
    network_unknown = any(item.network_performed is None for item in attempts)
    network_performed = any(item.network_performed is True for item in attempts)
    relationships = Counter(item.relationship.value for item in result.manifest.acquisitions)
    media_types = Counter(item.media.detected_media_type for item in result.artifact_set.objects)
    return {
        "status": result.status.value,
        "execution_mode": (attempts[0].execution_mode.value if attempts else "offline_fixture"),
        "network_performed": None if network_unknown else network_performed,
        "network_status": (
            "unknown" if network_unknown else "performed" if network_performed else "not_performed"
        ),
        "task_id": result.task_id,
        "run_id": result.run_id,
        "selection_id": result.manifest.selection_id,
        "artifact_set_id": result.artifact_set.artifact_set_id,
        "artifact_set_hash": result.artifact_set.artifact_set_hash,
        "manifest_id": result.manifest.manifest_id,
        "manifest_hash": result.manifest.manifest_hash,
        "run_log_hash": result.run_log.run_log_hash,
        "metrics": result.metrics.model_dump(mode="json"),
        "relationships": dict(sorted(relationships.items())),
        "detected_media_types": dict(sorted(media_types.items())),
        "objects": [
            {
                "object_id": item.object_id,
                "byte_sha256": item.byte_sha256,
                "size_bytes": item.size_bytes,
                "artifact_kind": item.media.artifact_kind.value,
                "detected_media_type": item.media.detected_media_type,
                "immutable": item.immutable,
            }
            for item in result.artifact_set.objects
        ],
        "event_type": "artifact.download.completed",
        "stored_event_count": sum(
            item.event_type.value == "artifact.stored" for item in result.events
        ),
        "event_count": len(result.events),
        "output_hash": result.output_hash,
    }


def build_parse_plan_summary(result: ParsePlanningResult) -> dict[str, object]:
    """Render M08 routing facts without bytes, source text, URLs, or scientific values."""

    plan = result.plan
    formats = Counter(item.format_family.value for item in plan.classifications)
    dispositions = Counter(item.disposition.value for item in plan.routes)
    target_modules = Counter(
        item.target_module.value for item in plan.routes if item.target_module is not None
    )
    primary_parsers = Counter(
        item.primary_parser_id for item in plan.routes if item.primary_parser_id is not None
    )
    return {
        "status": result.status.value,
        "execution_mode": plan.runtime.execution_mode.value,
        "network_performed": False,
        "model_classification_performed": False,
        "downstream_parser_executions": 0,
        "bronze_writes": 0,
        "task_id": result.task_id,
        "run_id": result.run_id,
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "artifact_set_hash": plan.artifact_set_hash,
        "manifest_hash": plan.manifest_hash,
        "capability_registry_hash": plan.capability_registry.registry_hash,
        "runtime_hash": plan.runtime.runtime_hash,
        "policy_hash": plan.policy_hash,
        "metrics": result.metrics.model_dump(mode="json"),
        "format_families": dict(sorted(formats.items())),
        "route_dispositions": dict(sorted(dispositions.items())),
        "target_modules": dict(sorted(target_modules.items())),
        "primary_parsers": dict(sorted(primary_parsers.items())),
        "fallback_count": sum(len(item.fallback_parser_ids) for item in plan.routes),
        "event_type": result.event.event_type.value,
        "event_count": 1,
        "output_hash": result.output_hash,
    }


def build_document_summary(result: DocumentParsingResult) -> dict[str, object]:
    """Render M09 audit facts without document text, URLs, filenames, or scientific values."""

    route_statuses = Counter(item.status.value for item in result.route_results)
    attempt_statuses = Counter(item.status.value for item in result.attempts)
    parser_attempts = Counter(item.parser_id for item in result.attempts)
    quality_checks: dict[str, dict[str, int]] = {}
    for attempt in result.attempts:
        for quality in attempt.quality_results:
            counts = quality_checks.setdefault(quality.kind.value, {"passed": 0, "failed": 0})
            counts["passed" if quality.passed else "failed"] += 1
    return {
        "status": result.status.value,
        "execution_mode": result.runtime.execution_mode.value,
        "network_performed": result.metrics.network_attempt_count > 0,
        "model_performed": result.metrics.model_attempt_count > 0,
        "bronze_writes": 0,
        "m10_table_executions": 0,
        "m11_chart_executions": 0,
        "m13_field_extractions": 0,
        "task_id": result.task_id,
        "run_id": result.run_id,
        "upstream_plan_id": result.upstream_plan_id,
        "upstream_plan_hash": result.upstream_plan_hash,
        "route_result_set_hash": result.route_result_set_hash,
        "ir_set_hash": result.ir_set_hash,
        "policy_hash": result.policy_hash,
        "runtime_hash": result.runtime.runtime_hash,
        "metrics": result.metrics.model_dump(mode="json"),
        "route_statuses": dict(sorted(route_statuses.items())),
        "attempt_statuses": dict(sorted(attempt_statuses.items())),
        "parser_attempts": dict(sorted(parser_attempts.items())),
        "blocked_parsers": sorted(
            item.parser_id for item in result.attempts if item.status.value == "blocked"
        ),
        "quality_checks": dict(sorted(quality_checks.items())),
        "event_type": result.event.event_type.value,
        "event_count": 1,
        "input_hash": result.input_hash,
        "output_hash": result.output_hash,
    }


def build_table_summary(result: TableParsingResult) -> dict[str, object]:
    """Render M10 structure and evidence counts without exposing any cell content."""

    route_statuses = Counter(item.status.value for item in result.route_results)
    attempt_statuses = Counter(item.status.value for item in result.attempts)
    quality_checks: dict[str, dict[str, int]] = {}
    for table in result.tables:
        for check in table.quality.checks:
            counts = quality_checks.setdefault(check.kind.value, {"passed": 0, "failed": 0})
            counts["passed" if check.passed else "failed"] += 1
    return {
        "status": result.status.value,
        "execution_mode": result.runtime.execution_mode.value,
        "network_performed": result.metrics.network_attempt_count > 0,
        "model_performed": result.metrics.model_attempt_count > 0,
        "bronze_writes": 0,
        "m13_field_extractions": 0,
        "task_id": result.task_id,
        "run_id": result.run_id,
        "upstream_plan_id": result.upstream_plan_id,
        "upstream_plan_hash": result.upstream_plan_hash,
        "route_result_set_hash": result.route_result_set_hash,
        "table_set_hash": result.table_set_hash,
        "policy_hash": result.policy_hash,
        "runtime_hash": result.runtime.runtime_hash,
        "metrics": result.metrics.model_dump(mode="json"),
        "route_statuses": dict(sorted(route_statuses.items())),
        "attempt_statuses": dict(sorted(attempt_statuses.items())),
        "parser_attempts": dict(
            sorted(Counter(item.parser_id for item in result.attempts).items())
        ),
        "quality_checks": dict(sorted(quality_checks.items())),
        "event_type": result.event.event_type.value,
        "event_count": 1,
        "input_hash": result.input_hash,
        "output_hash": result.output_hash,
    }


def build_extraction_summary(result: ExtractionResult) -> dict[str, object]:
    """Render M13 evidence coverage without raw values, lexemes, URLs, or source content."""

    return {
        "status": result.status.value,
        "execution_mode": result.runtime.execution_mode.value,
        "network_performed": False,
        "model_performed": False,
        "gold_writes": 0,
        "m14_mapping_executions": 0,
        "task_id": result.task_id,
        "run_id": result.run_id,
        "contract_id": result.contract_id,
        "contract_hash": result.contract_hash,
        "upstream_table_output_hash": result.upstream_table_output_hash,
        "evidence_set_id": result.evidence_set.evidence_set_id,
        "evidence_set_hash": result.evidence_set.evidence_set_hash,
        "candidate_set_id": result.candidate_set.candidate_set_id,
        "candidate_set_hash": result.candidate_set.candidate_set_hash,
        "metrics": result.metrics.model_dump(mode="json"),
        "candidate_fields": dict(
            sorted(Counter(item.field_name for item in result.candidate_set.candidates).items())
        ),
        "candidate_origins": dict(
            sorted(Counter(item.origin.value for item in result.candidate_set.candidates).items())
        ),
        "evidence_source_kinds": dict(
            sorted(Counter(item.source_kind.value for item in result.evidence_set.atoms).items())
        ),
        "gap_codes": dict(sorted(Counter(item.code.value for item in result.gaps).items())),
        "event_type": result.event.event_type.value,
        "event_count": 1,
        "input_hash": result.input_hash,
        "output_hash": result.output_hash,
    }


def build_mapping_summary(result: MappingResult) -> dict[str, object]:
    """Render M14 mapping gates without raw values, headers, URLs, or source content."""

    return {
        "status": result.status.value,
        "execution_mode": result.runtime.execution_mode.value,
        "network_performed": False,
        "model_performed": False,
        "embedding_performed": False,
        "gold_writes": 0,
        "m15_normalization_executions": 0,
        "task_id": result.task_id,
        "run_id": result.run_id,
        "contract_id": result.contract_id,
        "contract_hash": result.contract_hash,
        "upstream_extraction_output_hash": result.upstream_extraction_output_hash,
        "mapping_set_id": result.mapping_set.mapping_set_id,
        "mapping_set_hash": result.mapping_set.mapping_set_hash,
        "unmapped_set_id": result.unmapped_set.unmapped_set_id,
        "unmapped_set_hash": result.unmapped_set.unmapped_set_hash,
        "metrics": result.metrics.model_dump(mode="json"),
        "mapping_methods": dict(
            sorted(Counter(item.method.value for item in result.mapping_set.mappings).items())
        ),
        "mapping_decisions": dict(
            sorted(Counter(item.decision.value for item in result.mapping_set.mappings).items())
        ),
        "eligible_target_fields": dict(
            sorted(
                Counter(
                    item.target_field_name
                    for item in result.mapping_set.mappings
                    if item.eligible_for_m15
                ).items()
            )
        ),
        "unmapped_reasons": dict(
            sorted(Counter(item.reason.value for item in result.unmapped_set.fields).items())
        ),
        "event_type": result.event.event_type.value,
        "event_count": 1,
        "input_hash": result.input_hash,
        "output_hash": result.output_hash,
    }


def build_normalization_summary(result: NormalizationResult) -> dict[str, object]:
    """Render M15 traceability gates without raw or normalized scientific values."""

    fields = tuple(field for record in result.record_set.records for field in record.fields)
    return {
        "status": result.status.value,
        "execution_mode": result.runtime.execution_mode.value,
        "network_performed": False,
        "model_performed": False,
        "llm_value_mutations": 0,
        "gold_writes": 0,
        "m16_conflict_executions": 0,
        "task_id": result.task_id,
        "run_id": result.run_id,
        "contract_id": result.contract_id,
        "contract_hash": result.contract_hash,
        "upstream_mapping_output_hash": result.upstream_mapping_output_hash,
        "record_set_id": result.record_set.record_set_id,
        "record_set_hash": result.record_set.record_set_hash,
        "transformation_set_id": result.transformation_set.transformation_set_id,
        "transformation_set_hash": result.transformation_set.transformation_set_hash,
        "issue_set_id": result.issue_set.issue_set_id,
        "issue_set_hash": result.issue_set.issue_set_hash,
        "metrics": result.metrics.model_dump(mode="json"),
        "field_statuses": dict(sorted(Counter(item.status.value for item in fields).items())),
        "value_kinds": dict(
            sorted(Counter(item.value_kind.value for item in fields if item.value_kind).items())
        ),
        "transformation_kinds": dict(
            sorted(Counter(item.kind.value for item in result.transformation_set.records).items())
        ),
        "issue_codes": dict(
            sorted(Counter(item.code.value for item in result.issue_set.issues).items())
        ),
        "event_type": result.event.event_type.value,
        "event_count": 1,
        "input_hash": result.input_hash,
        "output_hash": result.output_hash,
    }


def _build_search_planning(
    goal: str, confirmed_by: str
) -> tuple[Phase1WorkflowResult, SearchPlanningResult | None]:
    workflow = build_offline_demo_workflow()
    phase1 = asyncio.run(
        workflow.execute(
            TaskIntakeRequest(
                research_goal=goal,
                allow_external_models=False,
            )
        )
    )
    if phase1.status is not Phase1Status.READY_FOR_CONFIRMATION or phase1.compilation is None:
        return phase1, None
    confirmed = workflow.confirm(
        contract_id=phase1.compilation.contract.contract_id,
        expected_contract_hash=phase1.compilation.contract.contract_hash,
        confirmed_by=confirmed_by,
    )
    if (
        confirmed.confirmation is None
        or confirmed.routing is None
        or confirmed.intake.envelope is None
    ):
        return confirmed, None
    registry = SourceCapabilityRegistryLoader.load_default()
    planning = SearchPlanner(
        registry=registry,
        available_source_ids=source_ids(registry),
    ).plan(
        SearchPlanningRequest(
            contract=confirmed.confirmation.contract,
            routing=confirmed.routing,
            budget_policy=confirmed.intake.envelope.budget_policy,
            capability_mode=SearchCapabilityMode.SIMULATED_DEMO,
        )
    )
    return confirmed, planning


async def _execute_offline_connectors(
    planning: SearchPlanningResult,
) -> ConnectorExecutionResult:
    connector_registry = load_default_connector_registry()
    capability_registry = SourceCapabilityRegistryLoader.load_default()
    bundle = build_offline_ia_connector_bundle(connector_registry)
    try:
        executor = ConnectorBatchExecutor(
            bundle.connectors,
            artifacts=bundle.artifacts,
            connector_registry=connector_registry,
            capability_registry=capability_registry,
        )
        return await executor.execute(
            ConnectorExecutionRequest(
                search_plan=planning.plan,
                runtime_snapshot=bundle.runtime_snapshot,
            )
        )
    finally:
        await bundle.aclose()


async def _execute_offline_selection(
    contract: ScientificDataContract,
    planning: SearchPlanningResult,
) -> SourceSelectionResult:
    connector_result = await _execute_offline_connectors(planning)
    return SourceSelectionService().select(
        SourceSelectionRequest(
            contract=contract,
            search_plan=planning.plan,
            connector_result=connector_result,
        )
    )


async def _execute_offline_artifacts(
    selection: SourceSelectionResult,
) -> ArtifactDownloadResult:
    _, result = await _execute_offline_artifacts_with_request(
        selection,
        store=MemoryBronzeStore(),
    )
    return result


async def _execute_offline_artifacts_with_request(
    selection: SourceSelectionResult,
    *,
    store: BronzeByteStore,
) -> tuple[ArtifactDownloadRequest, ArtifactDownloadResult]:
    """Execute M07 while retaining the exact request and read-only Bronze store for M08."""

    bundle = build_offline_ia_artifact_bundle(selection.selected_source_set)
    request = ArtifactDownloadRequest(
        selected_source_set=selection.selected_source_set,
        policy=bundle.policy,
        runtime=bundle.runtime,
        approvals=bundle.approvals,
        requested_at=bundle.runtime.checked_at,
    )
    service = ArtifactDownloadService(
        store=store,
        transport=bundle.transport,
    )
    try:
        return request, await service.execute(request)
    finally:
        await service.aclose()


async def _execute_offline_extraction(
    contract: ScientificDataContract,
    planning: SearchPlanningResult,
) -> tuple[ExtractionRequest, ExtractionResult, MemoryBronzeStore]:
    """Execute the exact offline M00-M13 tail and retain its immutable request chain."""

    selection = await _execute_offline_selection(contract, planning)
    store = MemoryBronzeStore()
    download_request, download_result = await _execute_offline_artifacts_with_request(
        selection,
        store=store,
    )
    parse_bundle = build_offline_parse_planning_bundle()
    parse_request = ParsePlanningRequest(
        contract=contract,
        download_request=download_request,
        download_result=download_result,
        capability_registry=parse_bundle.registry,
        policy=parse_bundle.policy,
        runtime=parse_bundle.runtime,
        requested_at=parse_bundle.runtime.checked_at,
    )
    parse_result = await ParsePlanningService(store=store).execute(parse_request)
    table_bundle = build_offline_table_parsing_bundle(
        parse_result.plan.capability_registry,
        parse_result.plan.runtime,
    )
    table_request = TableParsingRequest(
        parse_planning_request=parse_request,
        parse_planning_result=parse_result,
        policy=table_bundle.policy,
        runtime=table_bundle.runtime,
        requested_at=table_bundle.runtime.checked_at,
    )
    table_result = await TableParsingService(bronze_store=store).execute(table_request)
    extraction_bundle = build_offline_extraction_bundle(not_before=table_result.created_at)
    extraction_request = ExtractionRequest(
        contract=contract,
        table_parsing_request=table_request,
        table_parsing_result=table_result,
        policy=extraction_bundle.policy,
        runtime=extraction_bundle.runtime,
        requested_at=extraction_bundle.runtime.checked_at,
    )
    extraction_result = await EvidenceFirstExtractionService(bronze_store=store).execute(
        extraction_request
    )
    return extraction_request, extraction_result, store


async def _execute_offline_mapping(
    contract: ScientificDataContract,
    planning: SearchPlanningResult,
) -> tuple[MappingRequest, MappingResult, MemoryBronzeStore]:
    """Execute M14 over the exact offline M13 result without network or model calls."""

    extraction_request, extraction_result, store = await _execute_offline_extraction(
        contract,
        planning,
    )
    bundle = build_offline_mapping_bundle(not_before=extraction_result.created_at)
    request = MappingRequest(
        extraction_request=extraction_request,
        extraction_result=extraction_result,
        policy=bundle.policy,
        runtime=bundle.runtime,
        requested_at=bundle.runtime.checked_at,
    )
    result = await FieldMappingService(bronze_store=store).execute(request)
    return request, result, store


async def _execute_offline_normalization(
    contract: ScientificDataContract,
    planning: SearchPlanningResult,
) -> tuple[NormalizationRequest, NormalizationResult, MemoryBronzeStore]:
    """Execute M15 over the exact offline M14 result without network or model calls."""

    mapping_request, mapping_result, store = await _execute_offline_mapping(contract, planning)
    bundle = build_offline_normalization_bundle(not_before=mapping_result.created_at)
    request = NormalizationRequest(
        mapping_request=mapping_request,
        mapping_result=mapping_result,
        policy=bundle.policy,
        runtime=bundle.runtime,
        requested_at=bundle.runtime.checked_at,
    )
    result = await ScientificNormalizationService(bronze_store=store).execute(request)
    return request, result, store


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "doctor":
        try:
            report = build_doctor_report(Settings())
        except ValidationError as exc:
            print(
                json.dumps(
                    {
                        "status": "error",
                        "error": "invalid_configuration",
                        "details": exc.errors(include_context=False, include_input=False),
                    },
                    ensure_ascii=True,
                ),
                file=sys.stderr,
            )
            return 2
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return 0
    if args.command == "phase1-demo":
        try:
            workflow = build_offline_demo_workflow()
            result = asyncio.run(
                workflow.execute(
                    TaskIntakeRequest(
                        research_goal=args.goal,
                        allow_external_models=False,
                    )
                )
            )
            if args.confirmed_by is not None:
                if (
                    result.status is Phase1Status.READY_FOR_CONFIRMATION
                    and result.compilation is not None
                ):
                    result = workflow.confirm(
                        contract_id=result.compilation.contract.contract_id,
                        expected_contract_hash=result.compilation.contract.contract_hash,
                        confirmed_by=args.confirmed_by,
                    )
            print(json.dumps(build_phase1_summary(result), ensure_ascii=True, indent=2))
            return (
                0
                if result.status
                in {
                    Phase1Status.READY_FOR_CONFIRMATION,
                    Phase1Status.CONFIRMED,
                }
                else 3
            )
        except (AppError, RegistryLoadError, ValidationError) as exc:
            if isinstance(exc, AppError):
                code = exc.code.value
            elif isinstance(exc, RegistryLoadError):
                code = "configuration_error"
            else:
                code = "validation_failed"
            print(
                json.dumps(
                    {"status": "error", "error": code},
                    ensure_ascii=True,
                ),
                file=sys.stderr,
            )
            return 2
    if args.command == "phase2-plan-demo":
        try:
            phase1, planning = _build_search_planning(args.goal, args.confirmed_by)
            if planning is None:
                print(json.dumps(build_phase1_summary(phase1), ensure_ascii=True, indent=2))
                return 3
            print(json.dumps(build_search_plan_summary(planning), ensure_ascii=True, indent=2))
            return 0 if planning.status.value == "succeeded" else 3
        except (AppError, ValidationError) as exc:
            code = exc.code.value if isinstance(exc, AppError) else "validation_failed"
            print(
                json.dumps({"status": "error", "error": code}, ensure_ascii=True),
                file=sys.stderr,
            )
            return 2
    if args.command == "phase2-connect-demo":
        try:
            phase1, planning = _build_search_planning(args.goal, args.confirmed_by)
            if planning is None:
                print(json.dumps(build_phase1_summary(phase1), ensure_ascii=True, indent=2))
                return 3
            connector_result = asyncio.run(_execute_offline_connectors(planning))
            print(
                json.dumps(build_connector_summary(connector_result), ensure_ascii=True, indent=2)
            )
            return 0 if connector_result.status is ConnectorBatchStatus.SUCCEEDED else 3
        except (AppError, ValidationError) as exc:
            code = exc.code.value if isinstance(exc, AppError) else "validation_failed"
            print(
                json.dumps({"status": "error", "error": code}, ensure_ascii=True),
                file=sys.stderr,
            )
            return 2
    if args.command == "phase2-select-demo":
        try:
            phase1, planning = _build_search_planning(args.goal, args.confirmed_by)
            if planning is None or phase1.confirmation is None:
                print(json.dumps(build_phase1_summary(phase1), ensure_ascii=True, indent=2))
                return 3
            selection = asyncio.run(
                _execute_offline_selection(phase1.confirmation.contract, planning)
            )
            print(json.dumps(build_selection_summary(selection), ensure_ascii=True, indent=2))
            return (
                0
                if selection.status
                in {SourceSelectionStatus.SUCCEEDED, SourceSelectionStatus.PARTIAL}
                else 3
            )
        except (AppError, ValidationError) as exc:
            code = exc.code.value if isinstance(exc, AppError) else "validation_failed"
            print(
                json.dumps({"status": "error", "error": code}, ensure_ascii=True),
                file=sys.stderr,
            )
            return 2
    if args.command == "phase3-download-demo":
        try:
            phase1, planning = _build_search_planning(args.goal, args.confirmed_by)
            if planning is None or phase1.confirmation is None:
                print(json.dumps(build_phase1_summary(phase1), ensure_ascii=True, indent=2))
                return 3
            selection = asyncio.run(
                _execute_offline_selection(phase1.confirmation.contract, planning)
            )
            artifacts = asyncio.run(_execute_offline_artifacts(selection))
            print(json.dumps(build_artifact_summary(artifacts), ensure_ascii=True, indent=2))
            return (
                0
                if artifacts.status
                in {ArtifactDownloadStatus.SUCCEEDED, ArtifactDownloadStatus.PARTIAL}
                else 3
            )
        except (AppError, ValidationError) as exc:
            code = exc.code.value if isinstance(exc, AppError) else "validation_failed"
            print(
                json.dumps({"status": "error", "error": code}, ensure_ascii=True),
                file=sys.stderr,
            )
            return 2
    if args.command == "phase3-parse-plan-demo":
        try:
            phase1, planning = _build_search_planning(args.goal, args.confirmed_by)
            if planning is None or phase1.confirmation is None:
                print(json.dumps(build_phase1_summary(phase1), ensure_ascii=True, indent=2))
                return 3
            selection = asyncio.run(
                _execute_offline_selection(phase1.confirmation.contract, planning)
            )
            store = MemoryBronzeStore()
            download_request, download_result = asyncio.run(
                _execute_offline_artifacts_with_request(selection, store=store)
            )
            parse_bundle = build_offline_parse_planning_bundle()
            parse_request = ParsePlanningRequest(
                contract=phase1.confirmation.contract,
                download_request=download_request,
                download_result=download_result,
                capability_registry=parse_bundle.registry,
                policy=parse_bundle.policy,
                runtime=parse_bundle.runtime,
                requested_at=utc_now(),
            )
            parse_result = asyncio.run(ParsePlanningService(store=store).execute(parse_request))
            print(json.dumps(build_parse_plan_summary(parse_result), ensure_ascii=True, indent=2))
            return (
                0
                if parse_result.status
                in {ParsePlanningStatus.SUCCEEDED, ParsePlanningStatus.PARTIAL}
                else 3
            )
        except (AppError, RegistryLoadError, ValidationError) as exc:
            if isinstance(exc, AppError):
                code = exc.code.value
            elif isinstance(exc, RegistryLoadError):
                code = "configuration_error"
            else:
                code = "validation_failed"
            print(
                json.dumps({"status": "error", "error": code}, ensure_ascii=True),
                file=sys.stderr,
            )
            return 2
    if args.command == "phase3-document-demo":
        try:
            phase1, planning = _build_search_planning(args.goal, args.confirmed_by)
            if planning is None or phase1.confirmation is None:
                print(json.dumps(build_phase1_summary(phase1), ensure_ascii=True, indent=2))
                return 3
            selection = asyncio.run(
                _execute_offline_selection(phase1.confirmation.contract, planning)
            )
            store = MemoryBronzeStore()
            download_request, download_result = asyncio.run(
                _execute_offline_artifacts_with_request(selection, store=store)
            )
            parse_bundle = build_offline_parse_planning_bundle()
            parse_request = ParsePlanningRequest(
                contract=phase1.confirmation.contract,
                download_request=download_request,
                download_result=download_result,
                capability_registry=parse_bundle.registry,
                policy=parse_bundle.policy,
                runtime=parse_bundle.runtime,
                requested_at=parse_bundle.runtime.checked_at,
            )
            parse_result = asyncio.run(ParsePlanningService(store=store).execute(parse_request))
            document_bundle = build_offline_document_parsing_bundle(
                parse_result.plan.capability_registry,
                parse_result.plan.runtime,
            )
            document_request = DocumentParsingRequest(
                parse_planning_request=parse_request,
                parse_planning_result=parse_result,
                policy=document_bundle.policy,
                runtime=document_bundle.runtime,
                requested_at=document_bundle.runtime.checked_at,
            )
            document_result = asyncio.run(
                DocumentParsingService(bronze_store=store).execute(document_request)
            )
            print(json.dumps(build_document_summary(document_result), ensure_ascii=True, indent=2))
            return (
                0
                if document_result.status
                in {DocumentParsingStatus.SUCCEEDED, DocumentParsingStatus.PARTIAL}
                else 3
            )
        except (AppError, RegistryLoadError, ValidationError) as exc:
            if isinstance(exc, AppError):
                code = exc.code.value
            elif isinstance(exc, RegistryLoadError):
                code = "configuration_error"
            else:
                code = "validation_failed"
            print(
                json.dumps({"status": "error", "error": code}, ensure_ascii=True),
                file=sys.stderr,
            )
            return 2
    if args.command == "phase3-table-demo":
        try:
            phase1, planning = _build_search_planning(args.goal, args.confirmed_by)
            if planning is None or phase1.confirmation is None:
                print(json.dumps(build_phase1_summary(phase1), ensure_ascii=True, indent=2))
                return 3
            selection = asyncio.run(
                _execute_offline_selection(phase1.confirmation.contract, planning)
            )
            store = MemoryBronzeStore()
            download_request, download_result = asyncio.run(
                _execute_offline_artifacts_with_request(selection, store=store)
            )
            parse_bundle = build_offline_parse_planning_bundle()
            parse_request = ParsePlanningRequest(
                contract=phase1.confirmation.contract,
                download_request=download_request,
                download_result=download_result,
                capability_registry=parse_bundle.registry,
                policy=parse_bundle.policy,
                runtime=parse_bundle.runtime,
                requested_at=parse_bundle.runtime.checked_at,
            )
            parse_result = asyncio.run(ParsePlanningService(store=store).execute(parse_request))
            table_bundle = build_offline_table_parsing_bundle(
                parse_result.plan.capability_registry,
                parse_result.plan.runtime,
            )
            table_request = TableParsingRequest(
                parse_planning_request=parse_request,
                parse_planning_result=parse_result,
                policy=table_bundle.policy,
                runtime=table_bundle.runtime,
                requested_at=table_bundle.runtime.checked_at,
            )
            table_result = asyncio.run(
                TableParsingService(bronze_store=store).execute(table_request)
            )
            print(json.dumps(build_table_summary(table_result), ensure_ascii=True, indent=2))
            return (
                0
                if table_result.status in {TableParsingStatus.SUCCEEDED, TableParsingStatus.PARTIAL}
                else 3
            )
        except (AppError, RegistryLoadError, ValidationError) as exc:
            if isinstance(exc, AppError):
                code = exc.code.value
            elif isinstance(exc, RegistryLoadError):
                code = "configuration_error"
            else:
                code = "validation_failed"
            print(
                json.dumps({"status": "error", "error": code}, ensure_ascii=True),
                file=sys.stderr,
            )
            return 2
    if args.command == "phase4-extract-demo":
        try:
            phase1, planning = _build_search_planning(args.goal, args.confirmed_by)
            if planning is None or phase1.confirmation is None:
                print(json.dumps(build_phase1_summary(phase1), ensure_ascii=True, indent=2))
                return 3
            contract = phase1.confirmation.contract
            _, extraction_result, _ = asyncio.run(_execute_offline_extraction(contract, planning))
            print(
                json.dumps(
                    build_extraction_summary(extraction_result),
                    ensure_ascii=True,
                    indent=2,
                )
            )
            return (
                0
                if extraction_result.status
                in {ExtractionStatus.SUCCEEDED, ExtractionStatus.PARTIAL}
                else 3
            )
        except (AppError, RegistryLoadError, ValidationError) as exc:
            if isinstance(exc, AppError):
                code = exc.code.value
            elif isinstance(exc, RegistryLoadError):
                code = "configuration_error"
            else:
                code = "validation_failed"
            print(
                json.dumps({"status": "error", "error": code}, ensure_ascii=True),
                file=sys.stderr,
            )
            return 2
    if args.command == "phase4-map-demo":
        try:
            phase1, planning = _build_search_planning(args.goal, args.confirmed_by)
            if planning is None or phase1.confirmation is None:
                print(json.dumps(build_phase1_summary(phase1), ensure_ascii=True, indent=2))
                return 3
            _, mapping_result, _ = asyncio.run(
                _execute_offline_mapping(phase1.confirmation.contract, planning)
            )
            print(json.dumps(build_mapping_summary(mapping_result), ensure_ascii=True, indent=2))
            return (
                0
                if mapping_result.status in {MappingStatus.SUCCEEDED, MappingStatus.PARTIAL}
                else 3
            )
        except (AppError, RegistryLoadError, ValidationError) as exc:
            if isinstance(exc, AppError):
                code = exc.code.value
            elif isinstance(exc, RegistryLoadError):
                code = "configuration_error"
            else:
                code = "validation_failed"
            print(
                json.dumps({"status": "error", "error": code}, ensure_ascii=True),
                file=sys.stderr,
            )
            return 2
    if args.command == "phase4-normalize-demo":
        try:
            phase1, planning = _build_search_planning(args.goal, args.confirmed_by)
            if planning is None or phase1.confirmation is None:
                print(json.dumps(build_phase1_summary(phase1), ensure_ascii=True, indent=2))
                return 3
            _, normalization_result, _ = asyncio.run(
                _execute_offline_normalization(phase1.confirmation.contract, planning)
            )
            print(
                json.dumps(
                    build_normalization_summary(normalization_result),
                    ensure_ascii=True,
                    indent=2,
                )
            )
            return (
                0
                if normalization_result.status
                in {NormalizationStatus.SUCCEEDED, NormalizationStatus.PARTIAL}
                else 3
            )
        except (AppError, RegistryLoadError, ValidationError) as exc:
            if isinstance(exc, AppError):
                code = exc.code.value
            elif isinstance(exc, RegistryLoadError):
                code = "configuration_error"
            else:
                code = "validation_failed"
            print(
                json.dumps({"status": "error", "error": code}, ensure_ascii=True), file=sys.stderr
            )
            return 2
    return 2
