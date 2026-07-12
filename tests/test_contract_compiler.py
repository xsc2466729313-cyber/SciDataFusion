from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from typing import cast

import pytest
from pydantic import ValidationError

from scidatafusion.contracts.problem import (
    EntityIntent,
    ExtractionMethod,
    OutputFormat,
    OutputPreference,
    ProblemUnit,
    ScientificProblemSpec,
    ScopeDimension,
    ScopeIntent,
    SourceSpan,
    VariableIntent,
)
from scidatafusion.contracts.routing import (
    PackReference,
    RoutingDecision,
    RoutingMode,
    RoutingStatus,
)
from scidatafusion.contracts.scientific import (
    CanonicalField,
    CanonicalSchema,
    ContractAssumption,
    ContractCompilationResult,
    ContractConfirmationResult,
    ContractStatus,
    DataType,
    DerivationRule,
    FieldContract,
    FieldOrigin,
    FieldOriginKind,
    FieldRequirement,
    NumericRange,
    QualityGate,
    QualityGateKind,
    ScientificDataContract,
    SelectionConstraint,
    SelectionConstraintKind,
)
from scidatafusion.domain.registry import (
    DomainPackRegistry,
    RegistryErrorCode,
    RegistryLoadError,
    TaskPackRegistry,
    canonical_hash,
)
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.routing import DeterministicRouter
from scidatafusion.schema import ContractCompiler, ContractDiffService, SchemaPackRegistry
from scidatafusion.schema.registry import FieldTemplate, SchemaPack

_TASK_ID = "tsk_0123456789abcdef0123456789abcdef"
_RUN_ID = "run_fedcba9876543210fedcba9876543210"
_PROBLEM_ID = "prb_11111111111111111111111111111111"
_CREATED_AT = datetime(2026, 7, 12, 8, 0, tzinfo=UTC)


def _span(text: str, fragment: str) -> SourceSpan:
    start = text.index(fragment)
    return SourceSpan(start=start, end=start + len(fragment), text=fragment)


def _problem(
    goal: str,
    *,
    variable: str | None = "light curves",
    requested_unit: str | None = None,
    output_format: OutputFormat | None = None,
    temporal_scope: str | None = None,
) -> ScientificProblemSpec:
    whole = SourceSpan(start=0, end=len(goal), text=goal)
    spans = [whole]
    variables: tuple[VariableIntent, ...] = ()
    if variable is not None:
        variable_span = _span(goal, variable)
        spans.append(variable_span)
        variables = (
            VariableIntent(
                name=variable,
                requested_unit=requested_unit,
                confidence=1.0,
                evidence=(variable_span,),
                method=ExtractionMethod.USER_EXPLICIT,
                basis="Explicit variable in the test research goal.",
            ),
        )
    preferences: tuple[OutputPreference, ...] = ()
    if output_format is not None:
        format_span = _span(goal, output_format.value)
        spans.append(format_span)
        preferences = (
            OutputPreference(
                format=output_format,
                confidence=1.0,
                evidence=(format_span,),
                method=ExtractionMethod.USER_EXPLICIT,
                basis="Explicit output format in the test research goal.",
            ),
        )
    scope: ScopeIntent | None = None
    if temporal_scope is not None:
        scope_span = _span(goal, temporal_scope)
        spans.append(scope_span)
        scope = ScopeIntent(
            dimension=ScopeDimension.TEMPORAL,
            expression=temporal_scope,
            confidence=1.0,
            evidence=(scope_span,),
            method=ExtractionMethod.USER_EXPLICIT,
            basis="Explicit temporal scope in the test research goal.",
        )
    return ScientificProblemSpec(
        task_id=_TASK_ID,
        run_id=_RUN_ID,
        contract_version="1.0.0",
        created_at=_CREATED_AT,
        producer_version="1.0.0",
        problem_id=_PROBLEM_ID,
        raw_text=goal,
        research_goal=goal,
        research_questions=(goal,),
        problem_units=(
            ProblemUnit(
                unit_id="unit_1111111111111111",
                question=goal,
                confidence=1.0,
                evidence=(whole,),
                method=ExtractionMethod.USER_EXPLICIT,
                basis="Single explicit test research question.",
            ),
        ),
        target_variables=variables,
        temporal_scope=scope,
        output_preferences=preferences,
        source_spans=tuple(dict.fromkeys(spans)),
    )


def _all_capabilities() -> frozenset[str]:
    return (
        DomainPackRegistry.load_default().capabilities
        | TaskPackRegistry.load_default().capabilities
    )


def _route(problem: ScientificProblemSpec, *, formal: bool = True) -> RoutingDecision:
    router = DeterministicRouter(
        available_capabilities=_all_capabilities() if formal else frozenset()
    )
    return router.route_problem(
        problem,
        task_id=problem.task_id,
        run_id=problem.run_id,
        created_at=_CREATED_AT,
    )


@pytest.fixture
def ia_result() -> ContractCompilationResult:
    problem = _problem("Study Type Ia supernova light curves.")
    return ContractCompiler(clock=lambda: _CREATED_AT).compile(problem, _route(problem))


def test_compile_ia_golden_contract_and_views(ia_result: ContractCompilationResult) -> None:
    contract = ia_result.contract
    fields = {field.name: field for field in contract.fields}

    assert ia_result.status is ContractStatus.DRAFT
    assert ia_result.warnings == ()
    assert ia_result.conflicts == ()
    assert contract.contract_id == f"ctr_{contract.contract_hash[:32]}"
    assert set(fields) == {
        "band",
        "flux",
        "magnitude",
        "object_id",
        "observation_time",
        "source_record_id",
    }
    assert contract.entity_keys == ("object_id",)
    assert {
        name for name, field in fields.items() if field.requirement is FieldRequirement.REQUIRED
    } == {"band", "object_id", "observation_time", "source_record_id"}
    assert all(field.origins for field in fields.values())
    assert all(
        any(origin.kind is FieldOriginKind.PROBLEM for origin in fields[name].origins)
        for name in ("band", "flux", "magnitude", "observation_time")
    )
    assert any(gate.gate_id == "photometric_value_present" for gate in contract.quality_gates)
    assert ia_result.event.payload.output_hash == ia_result.output_hash
    assert ia_result.metrics.field_count == len(contract.fields)

    json_schema = ContractCompiler.json_schema(ia_result.canonical_schema)
    required = cast(list[str], json_schema["required"])
    assert json_schema["additionalProperties"] is False
    assert set(required) == {
        "band",
        "object_id",
        "observation_time",
        "source_record_id",
    }
    assert "magnitude" in ContractCompiler.render_markdown(contract)


def test_compile_freezes_evidence_grounded_research_concepts() -> None:
    goal = "Study Type Ia supernova light curves."
    problem = _problem(goal)
    entity_span = _span(goal, "Type Ia supernova")
    problem = ScientificProblemSpec.model_validate(
        {
            **problem.model_dump(),
            "target_entities": (
                EntityIntent(
                    name="Type Ia supernova",
                    entity_type="astronomical transient",
                    confidence=1.0,
                    evidence=(entity_span,),
                    method=ExtractionMethod.USER_EXPLICIT,
                    basis="Explicit research entity in the test goal.",
                ),
            ),
            "source_spans": (*problem.source_spans, entity_span),
        }
    )

    result = ContractCompiler(clock=lambda: _CREATED_AT).compile(problem, _route(problem))

    assert [(item.kind.value, item.term) for item in result.contract.research_concepts] == [
        ("entity", "Type Ia supernova"),
        ("variable", "light curves"),
    ]
    assert all(item.evidence_refs for item in result.contract.research_concepts)


def test_output_preference_and_temporal_scope_are_frozen_in_contract() -> None:
    goal = "Study Type Ia supernova light curves from 2010 to 2020 as parquet."
    problem = _problem(
        goal,
        output_format=OutputFormat.PARQUET,
        temporal_scope="2010 to 2020",
    )

    result = ContractCompiler(clock=lambda: _CREATED_AT).compile(problem, _route(problem))

    assert result.contract.output_formats == ("parquet",)
    assert len(result.contract.selection_constraints) == 1
    constraint = result.contract.selection_constraints[0]
    assert constraint.expression == "2010 to 2020"
    assert constraint.evidence_refs == (f"{_PROBLEM_ID}:42-54",)
    assert result.status is ContractStatus.DRAFT


def test_compile_replay_is_idempotent_and_force_recompute_keeps_semantic_identity() -> None:
    problem = _problem("Study Type Ia supernova light curves.")
    compiler = ContractCompiler(clock=lambda: _CREATED_AT)
    route = _route(problem)

    first = compiler.compile(problem, route)
    replay = compiler.compile(problem, route)
    recomputed = compiler.compile(problem, route, force_recompute=True)

    assert replay is first
    assert recomputed is not first
    assert recomputed.contract.contract_id == first.contract.contract_id
    assert recomputed.contract.contract_hash == first.contract.contract_hash
    assert recomputed.canonical_schema.schema_hash == first.canonical_schema.schema_hash
    assert recomputed.input_hash == first.input_hash
    assert recomputed.output_hash == first.output_hash
    assert recomputed.idempotency_key == first.idempotency_key


def test_confirmation_is_immutable_integrity_checked_and_idempotent(
    ia_result: ContractCompilationResult,
) -> None:
    compiler = ContractCompiler(clock=lambda: _CREATED_AT)
    original = ia_result.contract

    confirmed = compiler.confirm(
        original,
        expected_contract_hash=original.contract_hash,
        confirmed_by="reviewer@example.org",
    )
    replay = compiler.confirm(
        original,
        expected_contract_hash=original.contract_hash,
        confirmed_by="reviewer@example.org",
    )

    assert replay is confirmed
    assert original.status is ContractStatus.DRAFT
    assert confirmed.contract.status is ContractStatus.CONFIRMED
    assert confirmed.contract.contract_hash == original.contract_hash
    assert confirmed.event.occurred_at == confirmed.contract.confirmed_at

    with pytest.raises(AppError) as wrong_hash:
        ContractCompiler().confirm(
            original,
            expected_contract_hash="0" * 64,
            confirmed_by="reviewer@example.org",
        )
    assert wrong_hash.value.code is ErrorCode.VALIDATION_FAILED

    tampered = original.model_dump()
    tampered["fields"][0]["description"] = "Tampered after compilation."
    tampered_contract = ScientificDataContract.model_validate(tampered)
    with pytest.raises(AppError) as integrity_error:
        ContractCompiler().confirm(
            tampered_contract,
            expected_contract_hash=tampered_contract.contract_hash,
            confirmed_by="reviewer@example.org",
        )
    assert integrity_error.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR


def test_contract_compiler_confirmation_compare_and_set_is_thread_safe(
    ia_result: ContractCompilationResult,
) -> None:
    compiler = ContractCompiler(clock=lambda: _CREATED_AT)
    contract = ia_result.contract
    barrier = Barrier(2)

    def attempt(reviewer: str) -> tuple[str, str]:
        barrier.wait()
        try:
            result = compiler.confirm(
                contract,
                expected_contract_hash=contract.contract_hash,
                confirmed_by=reviewer,
            )
        except AppError as exc:
            return "error", exc.code.value
        return "ok", result.event.event_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(attempt, ("reviewer-a", "reviewer-b")))

    assert sum(status == "ok" for status, _ in outcomes) == 1
    assert sum(status == "error" for status, _ in outcomes) == 1


def test_unresolved_variable_stays_string_and_blocks_confirmation() -> None:
    goal = "Study Type Ia supernova light curves and nickel mass."
    problem = _problem(goal, variable="nickel mass", requested_unit="solar_mass")
    compiler = ContractCompiler(clock=lambda: _CREATED_AT)

    result = compiler.compile(problem, _route(problem))
    unresolved = next(
        field for field in result.contract.fields if field.name.startswith("requested_field_")
    )

    assert result.status is ContractStatus.NEEDS_REVIEW
    assert unresolved.requirement is FieldRequirement.REQUIRED
    assert unresolved.data_type is DataType.STRING
    assert unresolved.unit_dimension is None
    assert unresolved.allowed_units == ()
    assert unresolved.target_unit is None
    assert unresolved.valid_range is None
    assert unresolved.derivation is None
    with pytest.raises(AppError) as blocked:
        compiler.confirm(
            result.contract,
            expected_contract_hash=result.contract.contract_hash,
            confirmed_by="reviewer@example.org",
        )
    assert blocked.value.code is ErrorCode.QUALITY_GATE_FAILED


def test_nonformal_route_can_only_produce_needs_review() -> None:
    problem = _problem("Study Type Ia supernova light curves.")
    route = _route(problem, formal=False)

    result = ContractCompiler(clock=lambda: _CREATED_AT).compile(problem, route)

    assert route.status is RoutingStatus.UNSUPPORTED
    assert route.pack_selection.mode is RoutingMode.UNSUPPORTED
    assert result.status is ContractStatus.NEEDS_REVIEW
    assert any("not a succeeded formal route" in warning for warning in result.warnings)


def test_multitask_schema_conflict_retains_both_definitions() -> None:
    goal = "Integrate spatiotemporal Type Ia supernova light curves across station observations."
    problem = _problem(goal)
    route = _route(problem)

    result = ContractCompiler(clock=lambda: _CREATED_AT).compile(problem, route)

    assert route.status is RoutingStatus.SUCCEEDED
    assert route.pack_selection.mode is RoutingMode.FORMAL
    conflict = next(item for item in result.conflicts if item.field_name == "observation_time")
    assert result.status is ContractStatus.NEEDS_REVIEW
    assert conflict.existing_definition.data_type is DataType.NUMBER
    assert conflict.incoming_definition.data_type is DataType.DATETIME
    assert conflict.existing_definition != conflict.incoming_definition
    selected = next(field for field in result.contract.fields if field.name == "observation_time")
    assert selected.data_type is conflict.existing_definition.data_type
    assert conflict.existing_definition.origins[0] in selected.origins
    assert conflict.incoming_definition.origins[0] not in selected.origins


def _field(
    name: str,
    *,
    requirement: FieldRequirement = FieldRequirement.OPTIONAL,
    data_type: DataType = DataType.NUMBER,
    nullable: bool = True,
    aliases: tuple[str, ...] = (),
    valid_range: NumericRange | None = None,
    derivation: DerivationRule | None = None,
) -> FieldContract:
    return FieldContract(
        name=name,
        description=f"Test field {name}.",
        requirement=requirement,
        data_type=data_type,
        semantic_type=f"test_{name}",
        aliases=aliases,
        nullable=nullable,
        valid_range=valid_range,
        source_preference=("paper_table",),
        derivation=derivation,
        quality_threshold=0.9,
        origins=(
            FieldOrigin(
                kind=FieldOriginKind.DOMAIN_PACK,
                reference=f"test@1.0.0:{'a' * 64}",
                rationale="Declared by a test pack.",
            ),
        ),
    )


def test_compatible_merge_is_strict_and_json_schema_keeps_numeric_range() -> None:
    existing = _field("measurement", aliases=("value",))
    incoming = FieldContract.model_validate(
        {
            **existing.model_dump(),
            "requirement": FieldRequirement.REQUIRED,
            "nullable": False,
            "aliases": ("VALUE", "reading"),
            "quality_threshold": 0.99,
            "origins": (
                FieldOrigin(
                    kind=FieldOriginKind.TASK_PACK,
                    reference=f"measure@1.0.0:{'b' * 64}",
                    rationale="Declared by a test task pack.",
                ),
            ),
        }
    )

    merged = ContractCompiler._merge_compatible(existing, incoming)
    ranged = _field("bounded", valid_range=NumericRange(minimum=0.0, maximum=1.0))
    schema = ContractCompiler._canonical_schema(
        "ctr_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", (merged, ranged)
    )
    rendered = ContractCompiler.json_schema(schema)
    properties = cast(dict[str, dict[str, object]], rendered["properties"])

    assert merged.requirement is FieldRequirement.REQUIRED
    assert merged.nullable is False
    assert merged.aliases == ("value", "reading")
    assert merged.quality_threshold == 0.99
    assert len(merged.origins) == 2
    assert properties["bounded"]["minimum"] == 0.0
    assert properties["bounded"]["maximum"] == 1.0


def test_contract_invariants_reject_invalid_fields_and_derivation_cycles(
    ia_result: ContractCompilationResult,
) -> None:
    with pytest.raises(ValidationError, match="required fields cannot be nullable"):
        _field("bad", requirement=FieldRequirement.REQUIRED, nullable=True)
    with pytest.raises(ValidationError, match="derived fields require"):
        _field("bad", requirement=FieldRequirement.DERIVED)
    with pytest.raises(ValidationError, match="aliases must be unique"):
        _field("bad", aliases=("Value", "value"))
    with pytest.raises(ValidationError):
        NumericRange(minimum=float("nan"))

    record_id = _field(
        "record_id",
        requirement=FieldRequirement.REQUIRED,
        data_type=DataType.STRING,
        nullable=False,
    )
    derived_a = _field(
        "derived_a",
        requirement=FieldRequirement.DERIVED,
        derivation=DerivationRule(
            method="formula", expression="derived_b + 1", input_fields=("derived_b",)
        ),
    )
    derived_b = _field(
        "derived_b",
        requirement=FieldRequirement.DERIVED,
        derivation=DerivationRule(
            method="formula", expression="derived_a - 1", input_fields=("derived_a",)
        ),
    )
    payload = ia_result.contract.model_dump()
    payload.update(
        {
            "fields": (record_id, derived_a, derived_b),
            "entity_keys": ("record_id",),
            "quality_gates": (
                QualityGate(
                    gate_id="record_required",
                    kind=QualityGateKind.REQUIRED_FIELDS,
                    fields=("record_id",),
                    threshold=1.0,
                    description="Record id is required.",
                ),
            ),
        }
    )
    with pytest.raises(ValidationError, match="dependency cycles"):
        ScientificDataContract.model_validate(payload)


def test_result_linkage_rejects_tampered_metrics_and_event(
    ia_result: ContractCompilationResult,
) -> None:
    metrics_payload = ia_result.model_dump()
    metrics_payload["metrics"]["field_count"] += 1
    with pytest.raises(ValidationError, match="metrics must be derived"):
        ContractCompilationResult.model_validate(metrics_payload)

    event_payload = ia_result.model_dump()
    event_payload["event"]["payload"]["output_hash"] = "0" * 64
    with pytest.raises(ValidationError, match="event must refer"):
        ContractCompilationResult.model_validate(event_payload)


def test_contract_diff_reports_fields_and_metadata(ia_result: ContractCompilationResult) -> None:
    old = ia_result.contract
    new_fields = []
    for field in old.fields:
        if field.name == "magnitude":
            continue
        if field.name == "flux":
            field = FieldContract.model_validate({**field.model_dump(), "quality_threshold": 0.99})
        new_fields.append(field)
    new_fields.append(_field("color_index"))
    new_gates = tuple(
        QualityGate.model_validate({**gate.model_dump(), "fields": ("flux",)})
        if gate.gate_id == "photometric_value_present"
        else gate
        for gate in old.quality_gates
    )
    payload = old.model_dump()
    payload.update(
        {
            "contract_id": "ctr_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "version": "1.1.0",
            "fields": tuple(new_fields),
            "quality_gates": new_gates,
            "contract_hash": "b" * 64,
        }
    )
    new = ScientificDataContract.model_validate(payload)

    diff = ContractDiffService.compare(old, new)

    assert [(change.field_name, change.change) for change in diff.field_changes] == [
        ("color_index", "added"),
        ("flux", "changed"),
        ("magnitude", "removed"),
    ]
    assert [change.area for change in diff.metadata_changes] == ["quality_gates"]


def test_schema_registry_is_content_addressed_and_rejects_tampering(tmp_path: Path) -> None:
    registry = SchemaPackRegistry.load_default()
    astronomy = next(pack for pack in registry.packs if pack.name == "astronomy")
    valid_reference = PackReference(
        name=astronomy.name,
        pack_type=astronomy.pack_type,
        version=astronomy.version,
        content_hash=astronomy.source_pack_hash,
    )
    forged_reference = PackReference(
        name=astronomy.name,
        pack_type=astronomy.pack_type,
        version=astronomy.version,
        content_hash="0" * 64,
    )
    assert registry.get(valid_reference) is astronomy
    assert registry.get(forged_reference) is None

    raw = json.loads(
        Path("src/scidatafusion/registries/schema_packs.json").read_text(encoding="utf-8")
    )
    raw["packs"][0]["fields"][0]["description"] = "tampered"
    tampered_path = tmp_path / "tampered.json"
    tampered_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(RegistryLoadError) as mismatch:
        SchemaPackRegistry.from_file(tampered_path)
    assert mismatch.value.code is RegistryErrorCode.HASH_MISMATCH

    raw = json.loads(
        Path("src/scidatafusion/registries/schema_packs.json").read_text(encoding="utf-8")
    )
    raw["packs"][0]["unexpected"] = True
    raw["content_hash"] = canonical_hash(
        {"packs": raw["packs"], "registry_version": raw["registry_version"]}
    )
    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(RegistryLoadError) as invalid:
        SchemaPackRegistry.from_file(invalid_path)
    assert invalid.value.code is RegistryErrorCode.INVALID_SCHEMA


def test_field_contract_negative_invariants() -> None:
    with pytest.raises(ValidationError, match="minimum must not exceed"):
        NumericRange(minimum=2.0, maximum=1.0)
    with pytest.raises(ValidationError, match="input fields must be unique"):
        DerivationRule(method="formula", expression="x", input_fields=("x", "x"))

    base = _field("measurement").model_dump()
    invalid_updates = (
        ({"allowed_units": ("Jy", "Jy")}, "allowed_units must be unique"),
        ({"target_unit": "Jy"}, "target_unit must be included"),
        (
            {
                "derivation": DerivationRule(
                    method="formula", expression="x + 1", input_fields=("x",)
                )
            },
            "only derived fields",
        ),
        (
            {"data_type": DataType.STRING, "valid_range": NumericRange(minimum=0.0)},
            "only numeric fields",
        ),
        ({"source_preference": ("paper_table", "paper_table")}, "preferences must be unique"),
        ({"origins": (base["origins"][0], base["origins"][0])}, "origins must be unique"),
    )
    for update, message in invalid_updates:
        with pytest.raises(ValidationError, match=message):
            FieldContract.model_validate({**base, **update})

    with pytest.raises(ValidationError, match="quality gate fields must be unique"):
        QualityGate(
            gate_id="duplicate_fields",
            kind=QualityGateKind.ANY_OF_FIELDS,
            fields=("flux", "flux"),
            threshold=1.0,
            description="Invalid duplicate field gate.",
        )


def test_scientific_contract_negative_invariants(
    ia_result: ContractCompilationResult,
) -> None:
    contract = ia_result.contract
    base = contract.model_dump()
    fields = {field.name: field for field in contract.fields}

    invalid_updates: tuple[tuple[dict[str, object], str], ...] = (
        ({"fields": (contract.fields[0], contract.fields[0])}, "field names must be unique"),
        ({"entity_keys": ()}, "require at least one entity key"),
        ({"entity_keys": ("object_id", "object_id")}, "entity keys must be unique"),
        (
            {"domain_profile": ("astronomy", "astronomy")},
            "domain profile must be unique",
        ),
        ({"entity_keys": ("missing_key",)}, "entity key must reference"),
        ({"entity_keys": ("flux",)}, "entity key must be a required field"),
        (
            {"quality_gates": (contract.quality_gates[0], contract.quality_gates[0])},
            "quality gate ids must be unique",
        ),
        (
            {
                "quality_gates": (
                    QualityGate(
                        gate_id="unknown_field_gate",
                        kind=QualityGateKind.ANY_OF_FIELDS,
                        fields=("unknown_field",),
                        threshold=1.0,
                        description="References an unknown field.",
                    ),
                )
            },
            "only reference declared fields",
        ),
        (
            {
                "quality_gates": (
                    QualityGate(
                        gate_id="optional_required_gate",
                        kind=QualityGateKind.REQUIRED_FIELDS,
                        fields=("flux",),
                        threshold=1.0,
                        description="Incorrectly requires an optional field.",
                    ),
                )
            },
            "only reference required fields",
        ),
        ({"confirmed_by": "reviewer@example.org"}, "metadata must be complete"),
        (
            {"confirmed_by": "reviewer@example.org", "confirmed_at": _CREATED_AT},
            "only confirmed or superseded",
        ),
        ({"output_formats": ("csv", "csv")}, "output formats must be unique"),
    )
    for update, message in invalid_updates:
        with pytest.raises(ValidationError, match=message):
            ScientificDataContract.model_validate({**base, **update})

    constraint = SelectionConstraint(
        constraint_id="cst_1111111111111111",
        kind=SelectionConstraintKind.TEMPORAL_SCOPE,
        expression="2010 to 2020",
        evidence_refs=(f"{_PROBLEM_ID}:0-12",),
    )
    with pytest.raises(ValidationError, match="constraint ids must be unique"):
        ScientificDataContract.model_validate(
            {**base, "selection_constraints": (constraint, constraint)}
        )
    assumption = ContractAssumption(
        assumption_id="asm_1111111111111111",
        statement="Use public data only.",
        rationale="Test assumption.",
        source_status="proposed",
        evidence_refs=(f"{_PROBLEM_ID}:0-12",),
    )
    with pytest.raises(ValidationError, match="assumption ids must be unique"):
        ScientificDataContract.model_validate({**base, "assumptions": (assumption, assumption)})

    missing_dependency = _field(
        "derived_value",
        requirement=FieldRequirement.DERIVED,
        derivation=DerivationRule(
            method="formula", expression="missing + 1", input_fields=("missing",)
        ),
    )
    with pytest.raises(ValidationError, match="declared input fields"):
        ScientificDataContract.model_validate(
            {**base, "fields": (*contract.fields, missing_dependency)}
        )

    assert fields["flux"].requirement is FieldRequirement.OPTIONAL
    with pytest.raises(ValidationError, match="timestamps must include a timezone"):
        ScientificDataContract.model_validate(
            {**base, "created_at": _CREATED_AT.replace(tzinfo=None)}
        )


def test_canonical_contract_negative_invariants() -> None:
    required = CanonicalField(
        name="record_id",
        json_type="string",
        nullable=False,
        required=True,
        description="Record identifier.",
    )
    optional = CanonicalField(
        name="value",
        json_type="number",
        nullable=True,
        required=False,
        description="Optional value.",
    )
    with pytest.raises(ValidationError, match="minimum must not exceed"):
        CanonicalField(
            name="bad_range",
            json_type="number",
            nullable=True,
            required=False,
            description="Invalid range.",
            minimum=2.0,
            maximum=1.0,
        )
    with pytest.raises(ValidationError, match="only numeric canonical"):
        CanonicalField(
            name="bad_string_range",
            json_type="string",
            nullable=True,
            required=False,
            description="Invalid string range.",
            minimum=0.0,
        )

    base = {
        "schema_id": "sch_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "contract_id": "ctr_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "fields": (required, optional),
        "required_fields": ("record_id",),
        "schema_hash": "a" * 64,
    }
    invalid_updates: tuple[tuple[dict[str, object], str], ...] = (
        ({"fields": (required, required)}, "field names must be unique"),
        ({"required_fields": ("missing",)}, "must be declared"),
        ({"required_fields": ()}, "exactly match"),
        ({"required_fields": ("record_id", "record_id")}, "must be unique"),
    )
    for update, message in invalid_updates:
        with pytest.raises(ValidationError, match=message):
            CanonicalSchema.model_validate({**base, **update})


def test_compilation_and_confirmation_linkage_negative_invariants(
    ia_result: ContractCompilationResult,
) -> None:
    base = ia_result.model_dump()
    invalid_updates: tuple[tuple[dict[str, object], str], ...] = (
        ({"status": ContractStatus.CONFIRMED}, "must be draft or needs_review"),
        ({"warnings": ("review required",)}, "require needs_review status"),
        ({"task_id": "tsk_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}, "share result metadata"),
        (
            {
                "canonical_schema": {
                    **ia_result.canonical_schema.model_dump(),
                    "contract_id": "ctr_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                }
            },
            "must refer to the compiled contract",
        ),
        (
            {"created_at": _CREATED_AT.replace(tzinfo=None)},
            "timestamp must include a timezone",
        ),
    )
    for update, message in invalid_updates:
        with pytest.raises(ValidationError, match=message):
            ContractCompilationResult.model_validate({**base, **update})

    compiler = ContractCompiler(clock=lambda: _CREATED_AT)
    confirmation = compiler.confirm(
        ia_result.contract,
        expected_contract_hash=ia_result.contract.contract_hash,
        confirmed_by="reviewer@example.org",
    )
    invalid_confirmation = confirmation.model_dump()
    invalid_confirmation["event"]["payload"]["confirmed_by"] = "other@example.org"
    with pytest.raises(ValidationError, match="event must refer"):
        ContractConfirmationResult.model_validate(invalid_confirmation)

    with pytest.raises(AppError) as different_reviewer:
        compiler.confirm(
            ia_result.contract,
            expected_contract_hash=ia_result.contract.contract_hash,
            confirmed_by="other@example.org",
        )
    assert different_reviewer.value.code is ErrorCode.INVALID_REQUEST
    with pytest.raises(AppError) as confirmed_state:
        compiler.confirm(
            confirmation.contract,
            expected_contract_hash=confirmation.contract.contract_hash,
            confirmed_by="reviewer@example.org",
        )
    assert confirmed_state.value.code is ErrorCode.INVALID_REQUEST


def test_schema_template_and_pack_negative_invariants() -> None:
    registry = SchemaPackRegistry.load_default()
    astronomy = next(pack for pack in registry.packs if pack.name == "astronomy")
    template = astronomy.fields[0]
    base = template.model_dump()
    invalid_template_updates: tuple[tuple[dict[str, object], str], ...] = (
        ({"requirement": FieldRequirement.DERIVED}, "derivation exactly"),
        ({"aliases": ("ID", "id")}, "aliases must be unique"),
        ({"allowed_units": ("m", "m")}, "allowed_units must be unique"),
        ({"target_unit": "m"}, "target_unit must be allowed"),
        ({"nullable": True}, "cannot be nullable"),
        ({"valid_range": NumericRange(minimum=0.0)}, "only numeric schema"),
        (
            {"source_preference": ("paper_table", "paper_table")},
            "preferences must be unique",
        ),
    )
    for update, message in invalid_template_updates:
        with pytest.raises(ValidationError, match=message):
            FieldTemplate.model_validate({**base, **update})

    optional_id = FieldTemplate.model_validate(
        {**base, "requirement": FieldRequirement.OPTIONAL, "nullable": True}
    )
    unknown_gate = QualityGate(
        gate_id="unknown_pack_field",
        kind=QualityGateKind.ANY_OF_FIELDS,
        fields=("missing",),
        threshold=1.0,
        description="References a missing pack field.",
    )
    pack_base = astronomy.model_dump()
    invalid_pack_updates: tuple[tuple[dict[str, object], str], ...] = (
        ({"fields": (template, template)}, "field names must be unique"),
        ({"entity_keys": ("object_id", "object_id")}, "entity keys must be unique"),
        ({"entity_keys": ("missing",)}, "must reference local fields"),
        (
            {"fields": (optional_id,), "entity_keys": ("object_id",)},
            "must be required fields",
        ),
        (
            {"source_types": ("paper_table", "paper_table")},
            "source types must be unique",
        ),
        ({"intent_aliases": ("Light Curve", "light curve")}, "aliases must be unique"),
        ({"quality_gates": (unknown_gate,)}, "must reference local fields"),
    )
    for update, message in invalid_pack_updates:
        with pytest.raises(ValidationError, match=message):
            SchemaPack.model_validate({**pack_base, **update})

    light_curve = next(pack for pack in registry.packs if pack.name == "light_curve")
    gate = light_curve.quality_gates[0]
    with pytest.raises(ValidationError, match="gate ids must be unique"):
        SchemaPack.model_validate({**light_curve.model_dump(), "quality_gates": (gate, gate)})
    with pytest.raises(ValidationError, match="identities must be unique"):
        SchemaPackRegistry(
            registry_version=registry.registry_version,
            content_hash=registry.content_hash,
            packs=(astronomy, astronomy),
        )


def test_schema_registry_loader_structured_failures(tmp_path: Path) -> None:
    with pytest.raises(RegistryLoadError) as missing:
        SchemaPackRegistry.from_file(tmp_path / "missing.json")
    assert missing.value.code is RegistryErrorCode.NOT_FOUND

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    with pytest.raises(RegistryLoadError) as invalid_json:
        SchemaPackRegistry.from_file(malformed)
    assert invalid_json.value.code is RegistryErrorCode.INVALID_JSON

    too_large = tmp_path / "too-large.json"
    too_large.write_text(" " * (2 * 1024 * 1024 + 1), encoding="utf-8")
    with pytest.raises(RegistryLoadError) as oversized:
        SchemaPackRegistry.from_file(too_large)
    assert oversized.value.code is RegistryErrorCode.TOO_LARGE
