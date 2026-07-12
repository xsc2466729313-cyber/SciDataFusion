"""Local operational commands for the engineering baseline."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from scidatafusion import __version__
from scidatafusion.config import Settings
from scidatafusion.contracts.task import TaskIntakeRequest
from scidatafusion.contracts.workflow import Phase1Status, Phase1WorkflowResult
from scidatafusion.errors import AppError
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
    return 2
