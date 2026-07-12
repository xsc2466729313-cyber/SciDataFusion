"""M02 orchestration for replayable domain and task routing."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime
from threading import RLock
from typing import Protocol

from pydantic import BaseModel

from scidatafusion.contracts.routing import (
    EvidenceKind,
    RoutingDecision,
    RoutingMode,
    RoutingRequest,
    RoutingStatus,
)
from scidatafusion.domain.registry import (
    DomainPackRegistry,
    TaskPackRegistry,
    canonical_hash,
    combined_registry_hash,
)
from scidatafusion.routing._rules import (
    deterministic_hex,
    mask_routing_directives,
    unique_evidence,
)
from scidatafusion.routing.archetype import ArchetypeRouter, RuleBasedArchetypeRouter
from scidatafusion.routing.domain import DomainRouter, RuleBasedDomainRouter
from scidatafusion.routing.resolver import (
    DeterministicPackResolver,
    PackResolver,
    RoutingCalibrator,
)

_CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


class RoutingService(Protocol):
    """Public M02 service interface used by workflow adapters."""

    def route(
        self,
        request: RoutingRequest | str,
        *,
        available_capabilities: Iterable[str] | None = None,
        force_recompute: bool = False,
    ) -> RoutingDecision:
        """Return one validated, content-addressed routing decision."""


class DeterministicRouter:
    """Compose pure classifiers and a fail-closed pack resolver."""

    producer_version = "1.0.0"
    contract_version = "1.0.0"

    def __init__(
        self,
        *,
        domain_registry: DomainPackRegistry | None = None,
        task_registry: TaskPackRegistry | None = None,
        domain_router: DomainRouter | None = None,
        archetype_router: ArchetypeRouter | None = None,
        pack_resolver: PackResolver | None = None,
        available_capabilities: Iterable[str] | None = None,
    ) -> None:
        self.domain_registry = domain_registry or DomainPackRegistry.load_default()
        self.task_registry = task_registry or TaskPackRegistry.load_default()
        self.domain_router = domain_router or RuleBasedDomainRouter()
        self.archetype_router = archetype_router or RuleBasedArchetypeRouter()
        self.pack_resolver = pack_resolver or DeterministicPackResolver()
        self.available_capabilities = self._validate_capabilities(
            () if available_capabilities is None else available_capabilities
        )
        self.registry_hash = combined_registry_hash(self.domain_registry, self.task_registry)
        self._cache: dict[str, RoutingDecision] = {}
        self._cache_lock = RLock()

    def route(
        self,
        request: RoutingRequest | str,
        *,
        available_capabilities: Iterable[str] | None = None,
        force_recompute: bool = False,
    ) -> RoutingDecision:
        """Route once per replay key and return the cached immutable decision on retry."""

        validated_request = self._coerce_request(request)
        capabilities = (
            self.available_capabilities
            if available_capabilities is None
            else self._validate_capabilities(available_capabilities)
        )
        input_payload = {
            "context": list(validated_request.context),
            "research_goal": validated_request.research_goal,
        }
        input_hash = canonical_hash(input_payload)
        replay_key = deterministic_hex(
            validated_request.task_id,
            validated_request.run_id,
            "M02",
            self.contract_version,
            input_hash,
            self.producer_version,
            self.registry_hash,
            ",".join(sorted(capabilities)),
        )
        with self._cache_lock:
            if not force_recompute and replay_key in self._cache:
                return self._cache[replay_key]

        combined_text = "\n".join((validated_request.research_goal, *validated_request.context))
        safe_text, safety_evidence = mask_routing_directives(combined_text)
        domain_profile = self.domain_router.route(
            safe_text,
            self.domain_registry,
            prior_evidence=safety_evidence,
        )
        task_archetypes = self.archetype_router.route(
            safe_text,
            self.task_registry,
            prior_evidence=safety_evidence,
        )
        selection = self.pack_resolver.resolve(
            domain_profile,
            task_archetypes,
            self.domain_registry,
            self.task_registry,
            capabilities,
        )
        calibrator = RoutingCalibrator()
        confidence = calibrator.combined_confidence(domain_profile, task_archetypes)
        status, warnings = self._outcome(selection.mode, selection.missing_capabilities)
        if any(item.kind == EvidenceKind.SAFETY_FILTER for item in safety_evidence):
            warnings = (*warnings, "Routing-like instructions were ignored as untrusted data.")
        evidence = unique_evidence(
            (
                *domain_profile.evidence,
                *task_archetypes.evidence,
                *selection.evidence,
            )
        )
        semantic_payload = {
            "confidence": confidence,
            "contract_version": self.contract_version,
            "domain_profile": domain_profile.model_dump(mode="json"),
            "evidence": [item.model_dump(mode="json") for item in evidence],
            "fallback_path": list(selection.fallback_path),
            "input_hash": input_hash,
            "pack_selection": selection.model_dump(mode="json"),
            "producer_version": self.producer_version,
            "registry_hash": self.registry_hash,
            "status": status.value,
            "task_archetypes": task_archetypes.model_dump(mode="json"),
            "warnings": list(warnings),
        }
        decision = RoutingDecision(
            task_id=validated_request.task_id,
            run_id=validated_request.run_id,
            producer_version=self.producer_version,
            created_at=validated_request.created_at,
            status=status,
            input_hash=input_hash,
            registry_hash=self.registry_hash,
            replay_key=replay_key,
            decision_hash=canonical_hash(semantic_payload),
            confidence=confidence,
            domain_profile=domain_profile,
            task_archetypes=task_archetypes,
            pack_selection=selection,
            evidence=evidence,
            fallback_path=selection.fallback_path,
            warnings=warnings,
        )
        with self._cache_lock:
            existing = self._cache.get(replay_key)
            if not force_recompute and existing is not None:
                return existing
            self._cache[replay_key] = decision
        return decision

    def route_problem(
        self,
        problem_spec: BaseModel,
        *,
        task_id: str,
        run_id: str,
        created_at: datetime | None = None,
        available_capabilities: Iterable[str] | None = None,
    ) -> RoutingDecision:
        """Adapt a validated M01 Pydantic contract without importing its concrete class."""

        data = problem_spec.model_dump(mode="json")
        goal = data.get("research_goal")
        if not isinstance(goal, str):
            msg = "problem_spec must expose a validated research_goal string"
            raise TypeError(msg)
        context = self._problem_context(data)
        request = RoutingRequest(
            task_id=task_id,
            run_id=run_id,
            research_goal=goal,
            context=context,
            created_at=created_at or datetime.now(UTC),
        )
        return self.route(request, available_capabilities=available_capabilities)

    def _coerce_request(self, request: RoutingRequest | str) -> RoutingRequest:
        if isinstance(request, RoutingRequest):
            return request
        stripped = request.strip()
        seed = canonical_hash({"research_goal": stripped})
        return RoutingRequest(
            task_id=f"tsk_{seed[:32]}",
            run_id=f"run_{deterministic_hex(seed, self.registry_hash)[:32]}",
            research_goal=stripped,
        )

    @staticmethod
    def _validate_capabilities(values: Iterable[str]) -> frozenset[str]:
        capabilities = frozenset(values)
        invalid = sorted(
            value for value in capabilities if _CAPABILITY_PATTERN.fullmatch(value) is None
        )
        if invalid:
            msg = f"invalid capability names: {invalid!r}"
            raise ValueError(msg)
        return capabilities

    @staticmethod
    def _outcome(
        mode: RoutingMode,
        missing_capabilities: tuple[str, ...],
    ) -> tuple[RoutingStatus, tuple[str, ...]]:
        if mode == RoutingMode.UNSUPPORTED:
            return (
                RoutingStatus.UNSUPPORTED,
                (f"Required capabilities are unavailable: {', '.join(missing_capabilities)}",),
            )
        if mode == RoutingMode.PROVISIONAL:
            return (
                RoutingStatus.NEEDS_REVIEW,
                ("Low-confidence route uses only conservative generic packs.",),
            )
        if mode == RoutingMode.GENERIC:
            return (
                RoutingStatus.PARTIAL,
                (
                    "Specialist packs were not enabled because required capabilities are missing: "
                    f"{', '.join(missing_capabilities)}",
                ),
            )
        return RoutingStatus.SUCCEEDED, ()

    @staticmethod
    def _problem_context(data: dict[str, object]) -> tuple[str, ...]:
        values: list[str] = []
        questions = data.get("research_questions")
        if isinstance(questions, list):
            values.extend(item for item in questions if isinstance(item, str) and item.strip())
        for field in ("target_entities", "target_variables", "conditions"):
            items = data.get(field)
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, str) and item.strip():
                    values.append(item)
                elif isinstance(item, dict):
                    for key in ("name", "description", "raw_text", "value"):
                        value = item.get(key)
                        if isinstance(value, str) and value.strip():
                            values.append(value)
                            break
        return tuple(dict.fromkeys(value.strip()[:2000] for value in values))[:50]
