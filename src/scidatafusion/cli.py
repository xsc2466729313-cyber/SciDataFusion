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
from scidatafusion.contracts.connectors import (
    ConnectorBatchStatus,
    ConnectorExecutionRequest,
    ConnectorExecutionResult,
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
from scidatafusion.contracts.task import TaskIntakeRequest
from scidatafusion.contracts.workflow import Phase1Status, Phase1WorkflowResult
from scidatafusion.errors import AppError
from scidatafusion.search import SearchPlanner, SourceCapabilityRegistryLoader, source_ids
from scidatafusion.selection import SourceSelectionService
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
    bundle = build_offline_ia_artifact_bundle(selection.selected_source_set)
    request = ArtifactDownloadRequest(
        selected_source_set=selection.selected_source_set,
        policy=bundle.policy,
        runtime=bundle.runtime,
        approvals=bundle.approvals,
        requested_at=bundle.runtime.checked_at,
    )
    service = ArtifactDownloadService(
        store=MemoryBronzeStore(),
        transport=bundle.transport,
    )
    try:
        return await service.execute(request)
    finally:
        await service.aclose()


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
        except (AppError, ValidationError) as exc:
            code = exc.code.value if isinstance(exc, AppError) else "validation_failed"
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
    return 2
