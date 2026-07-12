from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from scidatafusion.artifacts.fixtures import build_offline_ia_artifact_bundle
from scidatafusion.artifacts.service import ArtifactDownloadService
from scidatafusion.artifacts.storage import BronzeByteStore, BronzeWriteReceipt, MemoryBronzeStore
from scidatafusion.cli import (
    _build_search_planning,
    _execute_offline_connectors,
    build_parse_plan_summary,
)
from scidatafusion.contracts.artifacts import (
    ArtifactDownloadRequest,
    ArtifactDownloadResult,
    BronzeObject,
)
from scidatafusion.contracts.parsing import (
    ArtifactClassification,
    ClassificationBasis,
    ParsePlanningExecutionMode,
    ParsePlanningPolicy,
    ParsePlanningRequest,
    ParsePlanningResult,
    ParsePlanningRuntimeSnapshot,
    ParsePlanningStatus,
    ParsePlanStatus,
    ParserCapabilityRegistry,
    ParseScope,
    ParseScopeKind,
    QualityCheckKind,
    RouteBlockerCode,
    RouteDisposition,
)
from scidatafusion.contracts.scientific import ScientificDataContract
from scidatafusion.contracts.selection import SourceSelectionRequest
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.parsing.checkpoints import (
    FileSystemParseCheckpointStore,
    MemoryParseCheckpointStore,
    ParseCheckpointStore,
)
from scidatafusion.parsing.classifier import (
    ArtifactClassifier,
    ClassificationDecision,
    DeterministicArtifactClassifier,
)
from scidatafusion.parsing.fixtures import build_offline_parse_planning_bundle
from scidatafusion.parsing.integrity import (
    calculate_parse_planning_idempotency_key,
    verify_parse_planning_integrity,
)
from scidatafusion.parsing.router import RegistryParseRouter, RouteDecision
from scidatafusion.parsing.service import (
    ParsePlanningService,
    _derive_entry_status,
    _derive_result_status,
)
from scidatafusion.selection import SourceSelectionService


@dataclass(frozen=True)
class _IaChain:
    contract: ScientificDataContract
    download_request: ArtifactDownloadRequest
    download_result: ArtifactDownloadResult
    store: MemoryBronzeStore
    parse_request: ParsePlanningRequest


@pytest.fixture(scope="module")
def ia_chain() -> _IaChain:
    """Build the real offline M00-M07 fixture and retain its Bronze store."""

    phase1, planning = _build_search_planning(
        "Study Type Ia supernova light curves using multi-source integration into CSV.",
        "m08-service-tests",
    )
    assert planning is not None
    assert phase1.confirmation is not None
    contract = phase1.confirmation.contract

    connector_result = asyncio.run(_execute_offline_connectors(planning))
    selection_time = max(
        contract.created_at,
        planning.created_at,
        connector_result.created_at,
    ) + timedelta(seconds=1)
    selected = (
        SourceSelectionService(clock=lambda: selection_time)
        .select(
            SourceSelectionRequest(
                contract=contract,
                search_plan=planning.plan,
                connector_result=connector_result,
            )
        )
        .selected_source_set
    )

    artifact_time = selected.created_at + timedelta(seconds=1)
    artifact_bundle = build_offline_ia_artifact_bundle(
        selected,
        clock=lambda: artifact_time,
    )
    download_request = ArtifactDownloadRequest(
        selected_source_set=selected,
        policy=artifact_bundle.policy,
        runtime=artifact_bundle.runtime,
        approvals=artifact_bundle.approvals,
        requested_at=artifact_time,
    )
    store = MemoryBronzeStore()
    download_service = ArtifactDownloadService(
        store=store,
        transport=artifact_bundle.transport,
        clock=lambda: artifact_time,
    )

    async def download() -> ArtifactDownloadResult:
        try:
            return await download_service.execute(download_request)
        finally:
            await download_service.aclose()

    download_result = asyncio.run(download())
    parse_time = download_result.created_at + timedelta(seconds=1)
    parse_bundle = build_offline_parse_planning_bundle(clock=lambda: parse_time)
    parse_request = ParsePlanningRequest(
        contract=contract,
        download_request=download_request,
        download_result=download_result,
        capability_registry=parse_bundle.registry,
        policy=parse_bundle.policy,
        runtime=parse_bundle.runtime,
        requested_at=parse_time,
    )
    return _IaChain(
        contract=contract,
        download_request=download_request,
        download_result=download_result,
        store=store,
        parse_request=parse_request,
    )


class _CountingClassifier:
    def __init__(self) -> None:
        self.calls = 0
        self._delegate = DeterministicArtifactClassifier()

    def classify(
        self,
        obj: BronzeObject,
        content: bytes,
        policy: ParsePlanningPolicy,
    ) -> ClassificationDecision:
        self.calls += 1
        return self._delegate.classify(obj, content, policy)


class _FailingClassifier:
    def __init__(self) -> None:
        self.calls = 0

    def classify(
        self,
        obj: BronzeObject,
        content: bytes,
        policy: ParsePlanningPolicy,
    ) -> ClassificationDecision:
        del obj, content, policy
        self.calls += 1
        raise AssertionError("checkpoint replay must not invoke the classifier")


class _InvalidClassifier:
    def classify(
        self,
        obj: BronzeObject,
        content: bytes,
        policy: ParsePlanningPolicy,
    ) -> ClassificationDecision:
        del obj, content, policy
        raise ValueError("invalid classifier output")


class _AppErrorClassifier:
    def classify(
        self,
        obj: BronzeObject,
        content: bytes,
        policy: ParsePlanningPolicy,
    ) -> ClassificationDecision:
        del obj, content, policy
        raise AppError(ErrorCode.QUALITY_GATE_FAILED, "classifier rejected sample")


class _EmptyRouter:
    def route(
        self,
        classification: ArtifactClassification,
        *,
        size_bytes: int,
        registry: ParserCapabilityRegistry,
        runtime: ParsePlanningRuntimeSnapshot,
        policy: ParsePlanningPolicy,
        remaining_cost_micro_usd: int,
    ) -> tuple[RouteDecision, ...]:
        del classification, size_bytes, registry, runtime, policy, remaining_cost_micro_usd
        return ()


class _InvalidRouter(_EmptyRouter):
    def route(
        self,
        classification: ArtifactClassification,
        *,
        size_bytes: int,
        registry: ParserCapabilityRegistry,
        runtime: ParsePlanningRuntimeSnapshot,
        policy: ParsePlanningPolicy,
        remaining_cost_micro_usd: int,
    ) -> tuple[RouteDecision, ...]:
        del classification, size_bytes, registry, runtime, policy, remaining_cost_micro_usd
        raise ValueError("invalid router output")


@dataclass(frozen=True)
class _UnknownQualityCheck:
    value: str = "unregistered_quality_check"


class _MalformedQualityCheckRouter(_EmptyRouter):
    def route(
        self,
        classification: ArtifactClassification,
        *,
        size_bytes: int,
        registry: ParserCapabilityRegistry,
        runtime: ParsePlanningRuntimeSnapshot,
        policy: ParsePlanningPolicy,
        remaining_cost_micro_usd: int,
    ) -> tuple[RouteDecision, ...]:
        decisions = RegistryParseRouter().route(
            classification,
            size_bytes=size_bytes,
            registry=registry,
            runtime=runtime,
            policy=policy,
            remaining_cost_micro_usd=remaining_cost_micro_usd,
        )
        return (
            replace(
                decisions[0],
                quality_checks=(cast(QualityCheckKind, _UnknownQualityCheck()),),
            ),
        )


class _MalformedBlockerRouter(_EmptyRouter):
    def route(
        self,
        classification: ArtifactClassification,
        *,
        size_bytes: int,
        registry: ParserCapabilityRegistry,
        runtime: ParsePlanningRuntimeSnapshot,
        policy: ParsePlanningPolicy,
        remaining_cost_micro_usd: int,
    ) -> tuple[RouteDecision, ...]:
        decisions = RegistryParseRouter().route(
            classification,
            size_bytes=size_bytes,
            registry=registry,
            runtime=runtime,
            policy=policy,
            remaining_cost_micro_usd=remaining_cost_micro_usd,
        )
        return (
            replace(
                decisions[0],
                blockers=(cast(RouteBlockerCode, "unregistered_blocker"),),
            ),
        )


class _MixedScopeRouter(_EmptyRouter):
    def route(
        self,
        classification: ArtifactClassification,
        *,
        size_bytes: int,
        registry: ParserCapabilityRegistry,
        runtime: ParsePlanningRuntimeSnapshot,
        policy: ParsePlanningPolicy,
        remaining_cost_micro_usd: int,
    ) -> tuple[RouteDecision, ...]:
        decisions = RegistryParseRouter().route(
            classification,
            size_bytes=size_bytes,
            registry=registry,
            runtime=runtime,
            policy=policy,
            remaining_cost_micro_usd=remaining_cost_micro_usd,
        )
        return (
            decisions[0],
            replace(
                decisions[0],
                scope=ParseScope(
                    kind=ParseScopeKind.PAGE_RANGE,
                    start_page=1,
                    end_page=1,
                ),
            ),
        )


class _CorruptingStore:
    def __init__(self, delegate: MemoryBronzeStore, corrupt_hash: str) -> None:
        self._delegate = delegate
        self._corrupt_hash = corrupt_hash

    def put(self, content: bytes) -> BronzeWriteReceipt:
        return self._delegate.put(content)

    def read(self, byte_sha256: str) -> bytes:
        content = self._delegate.read(byte_sha256)
        if byte_sha256 == self._corrupt_hash:
            return content + b"tampered"
        return content

    def contains(self, byte_sha256: str) -> bool:
        return self._delegate.contains(byte_sha256)


class _BlockingParsePlanningService(ParsePlanningService):
    def __init__(
        self,
        *,
        store: BronzeByteStore,
        classifier: ArtifactClassifier,
        checkpoints: ParseCheckpointStore,
    ) -> None:
        super().__init__(
            store=store,
            classifier=classifier,
            checkpoints=checkpoints,
        )
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def _execute_once(
        self,
        request: ParsePlanningRequest,
        *,
        input_hash: str,
        idempotency_key: str,
    ) -> ParsePlanningResult:
        self.entered.set()
        await self.release.wait()
        return await super()._execute_once(
            request,
            input_hash=input_hash,
            idempotency_key=idempotency_key,
        )


def _execute(
    chain: _IaChain,
    *,
    classifier: ArtifactClassifier | None = None,
    checkpoints: ParseCheckpointStore | None = None,
    store: BronzeByteStore | None = None,
) -> ParsePlanningResult:
    service = ParsePlanningService(
        store=store or chain.store,
        classifier=classifier,
        checkpoints=checkpoints,
    )
    return asyncio.run(service.execute(chain.parse_request))


def _assert_artifact_integrity_failure(
    chain: _IaChain,
    request: ParsePlanningRequest,
    *,
    store: BronzeByteStore | None = None,
) -> int:
    classifier = _CountingClassifier()
    service = ParsePlanningService(
        store=store or chain.store,
        classifier=classifier,
    )
    with pytest.raises(AppError) as caught:
        asyncio.run(service.execute(request))
    assert caught.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR
    assert classifier.calls == 0
    return classifier.calls


def test_ia_m00_m08_offline_chain_plans_unique_objects_without_execution(
    ia_chain: _IaChain,
) -> None:
    classifier = _CountingClassifier()
    result = _execute(ia_chain, classifier=classifier)
    plan = result.plan

    assert result.status.value == "succeeded"
    assert len(plan.source_objects) == 5
    assert len(plan.classifications) == 5
    assert len(plan.entries) == 5
    assert len(plan.routes) == 5
    assert len(plan.gaps) == 0
    assert classifier.calls == 5
    assert all(
        item.network_performed is False for item in ia_chain.download_result.run_log.attempts
    )

    csv_classification = next(
        item for item in plan.classifications if item.format_family.value == "csv"
    )
    csv_source = next(
        item for item in plan.source_objects if item.object_id == csv_classification.object_id
    )
    csv_entry = next(
        item for item in plan.entries if item.object_id == csv_classification.object_id
    )
    assert len(csv_classification.acquisition_ids) == 2
    assert len(csv_source.acquisition_ids) == 2
    assert csv_entry.classification_id == csv_classification.classification_id

    archive_route = next(
        item for item in plan.routes if item.disposition is RouteDisposition.METADATA_ONLY
    )
    assert archive_route.primary_parser_id is None
    pdf_route = next(item for item in plan.routes if item.primary_parser_id == "m09.pdf_text")
    assert pdf_route.fallback_parser_ids == ("m09.pdf_ocr",)

    assert plan.runtime.execution_mode is ParsePlanningExecutionMode.OFFLINE
    assert plan.runtime.model_classification_enabled is False
    assert plan.runtime.external_network_enabled is False
    assert result.metrics.model_candidate_classification_count == 0
    assert result.metrics.high_resource_primary_route_count == 0
    assert all(
        ClassificationBasis.MODEL_CANDIDATE not in item.basis for item in plan.classifications
    )
    summary = build_parse_plan_summary(result)
    assert summary["network_performed"] is False
    assert summary["model_classification_performed"] is False
    assert summary["downstream_parser_executions"] == 0
    verify_parse_planning_integrity(result, ia_chain.parse_request, ia_chain.store)


def test_repeated_concurrent_and_force_recompute_are_stable(ia_chain: _IaChain) -> None:
    async def scenario() -> tuple[
        ParsePlanningResult, ParsePlanningResult, ParsePlanningResult, int
    ]:
        classifier = _CountingClassifier()
        service = ParsePlanningService(store=ia_chain.store, classifier=classifier)
        first, second = await asyncio.gather(
            service.execute(ia_chain.parse_request),
            service.execute(ia_chain.parse_request),
        )
        replay = await service.execute(ia_chain.parse_request)
        forced = await service.execute(
            ia_chain.parse_request.model_copy(update={"force_recompute": True})
        )
        assert first is second
        assert replay is first
        assert forced is first
        assert forced.model_dump(mode="json") == first.model_dump(mode="json")
        return first, replay, forced, classifier.calls

    first, replay, forced, calls = asyncio.run(scenario())
    assert first is replay is forced
    assert calls == 10


def test_memory_checkpoint_replays_across_services_without_classifier(
    ia_chain: _IaChain,
) -> None:
    checkpoints = MemoryParseCheckpointStore()
    first_classifier = _CountingClassifier()
    first = _execute(ia_chain, classifier=first_classifier, checkpoints=checkpoints)
    follower_classifier = _FailingClassifier()
    follower = _execute(ia_chain, classifier=follower_classifier, checkpoints=checkpoints)

    assert follower is first
    assert follower.model_dump(mode="json") == first.model_dump(mode="json")
    assert first_classifier.calls == 5
    assert follower_classifier.calls == 0


def test_cancelled_follower_does_not_cancel_owner(ia_chain: _IaChain) -> None:
    async def scenario() -> tuple[ParsePlanningResult, ParsePlanningResult, int]:
        classifier = _CountingClassifier()
        service = _BlockingParsePlanningService(
            store=ia_chain.store,
            classifier=classifier,
            checkpoints=MemoryParseCheckpointStore(),
        )
        owner = asyncio.create_task(service.execute(ia_chain.parse_request))
        await service.entered.wait()
        follower = asyncio.create_task(service.execute(ia_chain.parse_request))
        await asyncio.sleep(0)
        follower.cancel()
        with pytest.raises(asyncio.CancelledError):
            await follower
        service.release.set()
        result = await owner
        replay = await service.execute(ia_chain.parse_request)
        return result, replay, classifier.calls

    result, replay, calls = asyncio.run(scenario())
    assert replay is result
    assert calls == 5


def test_filesystem_checkpoint_roundtrip_and_tampering(
    ia_chain: _IaChain,
    tmp_path: Path,
) -> None:
    checkpoints = FileSystemParseCheckpointStore(tmp_path / "roundtrip")
    first = _execute(ia_chain, classifier=_CountingClassifier(), checkpoints=checkpoints)
    replay_classifier = _FailingClassifier()
    replay = _execute(ia_chain, classifier=replay_classifier, checkpoints=checkpoints)
    assert replay.model_dump(mode="json") == first.model_dump(mode="json")
    assert replay_classifier.calls == 0

    target = next((tmp_path / "roundtrip").rglob("*.json"))
    target.write_text("{}", encoding="utf-8")
    with pytest.raises(AppError) as caught:
        _execute(ia_chain, classifier=_FailingClassifier(), checkpoints=checkpoints)
    assert caught.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR


def test_filesystem_checkpoint_guards_wrong_key_conflict_and_shard(
    ia_chain: _IaChain,
    tmp_path: Path,
) -> None:
    root = tmp_path / "guards"
    checkpoints = FileSystemParseCheckpointStore(root)
    result = _execute(ia_chain, checkpoints=checkpoints)
    assert checkpoints.save(result) == result

    conflict = result.model_copy(update={"output_hash": "f" * 64})
    with pytest.raises(AppError) as caught:
        checkpoints.save(conflict)
    assert caught.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR

    wrong_key = "e" * 64
    original = next(root.rglob("*.json"))
    wrong_target = root / wrong_key[:2] / f"{wrong_key}.json"
    wrong_target.parent.mkdir(parents=True, exist_ok=True)
    wrong_target.write_bytes(original.read_bytes())
    with pytest.raises(AppError) as caught:
        checkpoints.load(wrong_key)
    assert caught.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR

    collision_root = tmp_path / "collision"
    collision_store = FileSystemParseCheckpointStore(collision_root)
    shard = collision_root / result.idempotency_key[:2]
    shard.write_text("not a directory", encoding="utf-8")
    with pytest.raises(AppError) as caught:
        collision_store.save(result)
    assert caught.value.code is ErrorCode.INTERNAL_ERROR


def test_filesystem_checkpoint_rejects_symlink(ia_chain: _IaChain, tmp_path: Path) -> None:
    root = tmp_path / "symlink"
    checkpoints = FileSystemParseCheckpointStore(root)
    result = _execute(ia_chain, checkpoints=checkpoints)
    target = next(root.rglob("*.json"))
    real_file = tmp_path / "outside.json"
    real_file.write_bytes(target.read_bytes())
    target.unlink()
    try:
        target.symlink_to(real_file)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable in this Windows environment")
    with pytest.raises(AppError) as caught:
        checkpoints.load(result.idempotency_key)
    assert caught.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR


def test_request_integrity_tampering_fails_before_classifier(ia_chain: _IaChain) -> None:
    contract_tampered = ia_chain.contract.model_copy(update={"contract_hash": "f" * 64})
    _assert_artifact_integrity_failure(
        ia_chain,
        ia_chain.parse_request.model_copy(update={"contract": contract_tampered}),
    )

    result_tampered = ia_chain.download_result.model_copy(update={"output_hash": "f" * 64})
    _assert_artifact_integrity_failure(
        ia_chain,
        ia_chain.parse_request.model_copy(update={"download_result": result_tampered}),
    )

    registry_tampered = ia_chain.parse_request.capability_registry.model_copy(
        update={"registry_hash": "f" * 64}
    )
    _assert_artifact_integrity_failure(
        ia_chain,
        ia_chain.parse_request.model_copy(update={"capability_registry": registry_tampered}),
    )

    runtime_tampered = ia_chain.parse_request.runtime.model_copy(update={"runtime_hash": "f" * 64})
    _assert_artifact_integrity_failure(
        ia_chain,
        ia_chain.parse_request.model_copy(update={"runtime": runtime_tampered}),
    )

    corrupt_hash = ia_chain.download_result.artifact_set.objects[0].byte_sha256
    _assert_artifact_integrity_failure(
        ia_chain,
        ia_chain.parse_request,
        store=_CorruptingStore(ia_chain.store, corrupt_hash),
    )


def test_result_hash_tampering_fails_closed(ia_chain: _IaChain) -> None:
    result = _execute(ia_chain)

    tampered_results: list[ParsePlanningResult] = [
        result.model_copy(update={"output_hash": "f" * 64}),
    ]
    classification = result.plan.classifications[0].model_copy(
        update={"classification_hash": "f" * 64}
    )
    tampered_results.append(
        result.model_copy(
            update={
                "plan": result.plan.model_copy(
                    update={"classifications": (classification, *result.plan.classifications[1:])}
                )
            }
        )
    )
    route = result.plan.routes[0].model_copy(update={"route_hash": "f" * 64})
    tampered_results.append(
        result.model_copy(
            update={
                "plan": result.plan.model_copy(update={"routes": (route, *result.plan.routes[1:])})
            }
        )
    )
    plan = result.plan.model_copy(update={"plan_hash": "f" * 64})
    tampered_results.append(result.model_copy(update={"plan": plan}))

    for tampered in tampered_results:
        with pytest.raises(AppError) as caught:
            verify_parse_planning_integrity(tampered, ia_chain.parse_request, ia_chain.store)
        assert caught.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR


def test_checkpoint_replay_does_not_call_failing_classifier(ia_chain: _IaChain) -> None:
    checkpoints = MemoryParseCheckpointStore()
    _execute(ia_chain, checkpoints=checkpoints)
    failing = _FailingClassifier()
    replay = _execute(ia_chain, classifier=failing, checkpoints=checkpoints)
    assert replay.module_id == "M08"
    assert failing.calls == 0


@pytest.mark.parametrize(
    ("classifier", "expected_code"),
    [
        (_InvalidClassifier(), ErrorCode.ARTIFACT_INTEGRITY_ERROR),
        (_AppErrorClassifier(), ErrorCode.QUALITY_GATE_FAILED),
    ],
)
def test_classifier_failures_are_structured_and_never_checkpointed(
    ia_chain: _IaChain,
    classifier: ArtifactClassifier,
    expected_code: ErrorCode,
) -> None:
    checkpoints = MemoryParseCheckpointStore()
    service = ParsePlanningService(
        store=ia_chain.store,
        classifier=classifier,
        checkpoints=checkpoints,
    )

    with pytest.raises(AppError) as caught:
        asyncio.run(service.execute(ia_chain.parse_request))

    assert caught.value.code is expected_code
    key = calculate_parse_planning_idempotency_key(ia_chain.parse_request, "1.0.0")
    assert checkpoints.load(key) is None


@pytest.mark.parametrize(
    ("router", "expected_cause"),
    [
        pytest.param(_EmptyRouter(), None, id="empty"),
        pytest.param(_InvalidRouter(), ValueError, id="raised-value-error"),
        pytest.param(
            _MalformedQualityCheckRouter(),
            KeyError,
            id="returned-unknown-quality-check",
        ),
        pytest.param(
            _MalformedBlockerRouter(),
            ValidationError,
            id="returned-unknown-blocker",
        ),
    ],
)
def test_router_failures_are_structured_and_never_publish_a_result(
    ia_chain: _IaChain,
    router: _EmptyRouter,
    expected_cause: type[BaseException] | None,
) -> None:
    checkpoints = MemoryParseCheckpointStore()
    service = ParsePlanningService(
        store=ia_chain.store,
        router=router,
        checkpoints=checkpoints,
    )

    with pytest.raises(AppError) as caught:
        asyncio.run(service.execute(ia_chain.parse_request))

    assert caught.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR
    if expected_cause is not None:
        assert isinstance(caught.value.__cause__, expected_cause)
    key = calculate_parse_planning_idempotency_key(ia_chain.parse_request, "1.0.0")
    assert checkpoints.load(key) is None


def test_router_aggregate_scope_failure_is_structured_and_not_checkpointed(
    ia_chain: _IaChain,
) -> None:
    checkpoints = MemoryParseCheckpointStore()
    service = ParsePlanningService(
        store=ia_chain.store,
        router=_MixedScopeRouter(),
        checkpoints=checkpoints,
    )

    with pytest.raises(AppError) as caught:
        asyncio.run(service.execute(ia_chain.parse_request))

    assert caught.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR
    assert "invalid aggregate parse plan" in caught.value.message
    assert isinstance(caught.value.__cause__, ValidationError)
    key = calculate_parse_planning_idempotency_key(ia_chain.parse_request, "1.0.0")
    assert checkpoints.load(key) is None


@pytest.mark.parametrize(
    ("dispositions", "expected"),
    [
        ((RouteDisposition.NEEDS_REVIEW,), ParsePlanStatus.NEEDS_REVIEW),
        ((RouteDisposition.UNSUPPORTED,), ParsePlanStatus.UNSUPPORTED),
        ((RouteDisposition.FAILED,), ParsePlanStatus.FAILED),
        (
            (RouteDisposition.PARSE, RouteDisposition.NEEDS_REVIEW),
            ParsePlanStatus.PARTIAL,
        ),
    ],
)
def test_entry_status_is_derived_from_every_route_disposition(
    dispositions: tuple[RouteDisposition, ...],
    expected: ParsePlanStatus,
) -> None:
    assert _derive_entry_status(dispositions) is expected


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ((ParsePlanStatus.NEEDS_REVIEW,), ParsePlanningStatus.NEEDS_REVIEW),
        ((ParsePlanStatus.UNSUPPORTED,), ParsePlanningStatus.UNSUPPORTED),
        ((ParsePlanStatus.FAILED,), ParsePlanningStatus.FAILED),
        (
            (ParsePlanStatus.SUCCEEDED, ParsePlanStatus.NEEDS_REVIEW),
            ParsePlanningStatus.PARTIAL,
        ),
    ],
)
def test_aggregate_status_is_derived_from_every_entry_status(
    statuses: tuple[ParsePlanStatus, ...],
    expected: ParsePlanningStatus,
) -> None:
    assert _derive_result_status(statuses) is expected
