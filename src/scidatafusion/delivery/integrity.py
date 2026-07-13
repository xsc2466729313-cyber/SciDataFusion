"""Replay and integrity checks for M20 delivery results."""

from __future__ import annotations

import hashlib
import hmac
from typing import NoReturn

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.contracts.delivery import (
    DeliveryManifest,
    DeliveryRequest,
    DeliveryResult,
    DeliveryRuleDescriptor,
    DeliveryRuntimeSnapshot,
)
from scidatafusion.contracts.events import EventType
from scidatafusion.delivery.storage import DeliveryByteStore
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.knowledge.integrity import verify_knowledge_result


def calculate_delivery_policy_hash(request: DeliveryRequest) -> str:
    return canonical_hash(request.policy.model_dump(mode="json"))


def calculate_delivery_rule_hash(value: DeliveryRuleDescriptor) -> str:
    return canonical_hash(value.model_dump(mode="json", exclude={"rule_hash"}))


def calculate_delivery_runtime_hash(value: DeliveryRuntimeSnapshot) -> str:
    return canonical_hash(value.model_dump(mode="json", exclude={"runtime_hash"}))


def calculate_delivery_input_hash(request: DeliveryRequest) -> str:
    return canonical_hash(
        {
            "knowledge_input_hash": request.knowledge_result.input_hash,
            "knowledge_output_hash": request.knowledge_result.output_hash,
            "policy_hash": calculate_delivery_policy_hash(request),
            "runtime_hash": request.runtime.runtime_hash,
        }
    )


def calculate_delivery_idempotency_key(request: DeliveryRequest, producer_version: str) -> str:
    return canonical_hash(
        {
            "contract_version": request.knowledge_result.contract_version,
            "input_hash": calculate_delivery_input_hash(request),
            "module_id": "M20",
            "producer_version": producer_version,
            "task_id": request.knowledge_result.task_id,
        }
    )


def calculate_delivery_manifest_hash(value: DeliveryManifest) -> str:
    return canonical_hash(
        value.model_dump(mode="json", exclude={"manifest_id", "manifest_hash", "created_at"})
    )


def calculate_delivery_output_hash(value: DeliveryResult) -> str:
    return canonical_hash(
        value.model_dump(
            mode="json", exclude={"output_hash": True, "event": {"payload": {"output_hash"}}}
        )
    )


def calculate_delivery_event_id(key: str) -> str:
    return f"evt_{canonical_hash({'idempotency_key': key, 'type': 'delivery.completed'})[:32]}"


def verify_delivery_request(request: DeliveryRequest, bronze_store: BronzeByteStore) -> None:
    verify_knowledge_result(
        request.knowledge_result,
        request.knowledge_request,
        bronze_store,
    )
    if not hmac.compare_digest(
        request.runtime.rule.rule_hash,
        calculate_delivery_rule_hash(request.runtime.rule),
    ):
        _fail("M20 rule hash is invalid")
    if not hmac.compare_digest(
        request.runtime.runtime_hash,
        calculate_delivery_runtime_hash(request.runtime),
    ):
        _fail("M20 runtime hash is invalid")


def verify_delivery_result(
    result: DeliveryResult,
    request: DeliveryRequest,
    bronze_store: BronzeByteStore,
    delivery_store: DeliveryByteStore,
) -> None:
    verify_delivery_request(request, bronze_store)
    upstream = request.knowledge_result
    if not (
        result.task_id == upstream.task_id
        and result.run_id == upstream.run_id
        and result.contract_id == upstream.contract_id
        and result.contract_version == upstream.contract_version
        and result.policy == request.policy
        and result.policy_hash == calculate_delivery_policy_hash(request)
        and result.runtime == request.runtime
        and result.input_hash == calculate_delivery_input_hash(request)
        and result.idempotency_key
        == calculate_delivery_idempotency_key(request, result.producer_version)
        and result.event.event_id == calculate_delivery_event_id(result.idempotency_key)
        and result.event.event_type is EventType.DELIVERY_COMPLETED
        and result.event.causation_event_id == upstream.event.event_id
        and result.event.payload.upstream_knowledge_output_hash == upstream.output_hash
    ):
        _fail("M20 result does not match its immutable request")
    expected_manifest_hash = calculate_delivery_manifest_hash(result.manifest)
    if not (
        result.manifest.manifest_hash == expected_manifest_hash
        and result.manifest.manifest_id == f"dmf_{expected_manifest_hash[:32]}"
        and result.output_hash == calculate_delivery_output_hash(result)
    ):
        _fail("M20 manifest or output hash is invalid")
    for artifact in (*result.manifest.files, result.package):
        payload = delivery_store.get(artifact.sha256)
        if payload is None:
            _fail("M20 artifact bytes are missing")
        if not (
            len(payload) == artifact.size_bytes
            and artifact.artifact_id == f"dlf_{artifact.sha256[:32]}"
            and hmac.compare_digest(hashlib.sha256(payload).hexdigest(), artifact.sha256)
        ):
            _fail("M20 artifact identity or bytes are invalid")


def _fail(message: str) -> NoReturn:
    raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, message)
