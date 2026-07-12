"""Compatibility and capability resolution for candidate routing packs."""

from __future__ import annotations

from collections.abc import Set
from typing import Protocol

from scidatafusion.contracts.routing import (
    DomainProfile,
    EvidenceKind,
    PackSelection,
    RoutingEvidence,
    RoutingMode,
    TaskArchetypeSet,
)
from scidatafusion.domain.registry import (
    DomainPackManifest,
    DomainPackRegistry,
    TaskPackManifest,
    TaskPackRegistry,
)
from scidatafusion.routing._rules import make_evidence, unique_evidence


class PackResolver(Protocol):
    """Stable interface for deterministic pack dependency resolution."""

    def resolve(
        self,
        domain_profile: DomainProfile,
        task_archetypes: TaskArchetypeSet,
        domain_registry: DomainPackRegistry,
        task_registry: TaskPackRegistry,
        available_capabilities: Set[str],
    ) -> PackSelection:
        """Enable only compatible packs backed by healthy registered capabilities."""


class DeterministicPackResolver:
    """Resolve exact pack versions and fail closed on missing capabilities."""

    def resolve(
        self,
        domain_profile: DomainProfile,
        task_archetypes: TaskArchetypeSet,
        domain_registry: DomainPackRegistry,
        task_registry: TaskPackRegistry,
        available_capabilities: Set[str],
    ) -> PackSelection:
        """Return enabled/proposed pack references and an explicit fallback route."""

        specialist_domains = self._specialist_domains(domain_profile, domain_registry)
        specialist_tasks = self._specialist_tasks(task_archetypes, task_registry)
        confident = not domain_profile.provisional and not task_archetypes.provisional

        if confident:
            required = self._required_capabilities(specialist_domains, specialist_tasks)
            missing = tuple(sorted(required - available_capabilities))
            if not missing:
                evidence = self._selection_evidence(specialist_domains, specialist_tasks)
                return PackSelection(
                    mode=RoutingMode.FORMAL,
                    domain_packs=tuple(
                        domain_registry.reference(pack) for pack in specialist_domains
                    ),
                    task_packs=tuple(task_registry.reference(pack) for pack in specialist_tasks),
                    fallback_path=("generic_data_integration", "human_review"),
                    evidence=evidence,
                )
            return self._capability_fallback(
                specialist_domains,
                specialist_tasks,
                missing,
                domain_registry,
                task_registry,
                available_capabilities,
            )

        return self._provisional_fallback(
            specialist_domains,
            specialist_tasks,
            domain_registry,
            task_registry,
            available_capabilities,
        )

    @staticmethod
    def _specialist_domains(
        profile: DomainProfile,
        registry: DomainPackRegistry,
    ) -> tuple[DomainPackManifest, ...]:
        names = (profile.primary_domain, *profile.secondary_domains)
        return tuple(registry.require(name) for name in names if name != "generic")

    @staticmethod
    def _specialist_tasks(
        archetypes: TaskArchetypeSet,
        registry: TaskPackRegistry,
    ) -> tuple[TaskPackManifest, ...]:
        return tuple(
            registry.require(name)
            for name in archetypes.archetypes
            if name != "generic_data_integration"
        )

    @staticmethod
    def _required_capabilities(
        domains: tuple[DomainPackManifest, ...],
        tasks: tuple[TaskPackManifest, ...],
    ) -> frozenset[str]:
        required: set[str] = set()
        for domain_pack in domains:
            required.update(domain_pack.required_capabilities)
        for task_pack in tasks:
            required.update(task_pack.required_capabilities)
        return frozenset(required)

    @staticmethod
    def _selection_evidence(
        domains: tuple[DomainPackManifest, ...],
        tasks: tuple[TaskPackManifest, ...],
    ) -> tuple[RoutingEvidence, ...]:
        evidence: list[RoutingEvidence] = []
        for domain_pack in domains:
            evidence.append(
                make_evidence(
                    kind=EvidenceKind.CAPABILITY,
                    source="capability_registry",
                    target=domain_pack.name,
                    signal="all_required_capabilities_available",
                    weight=1.0,
                    rationale="Every required capability is registered for the selected pack.",
                )
            )
        for task_pack in tasks:
            evidence.append(
                make_evidence(
                    kind=EvidenceKind.CAPABILITY,
                    source="capability_registry",
                    target=task_pack.name,
                    signal="all_required_capabilities_available",
                    weight=1.0,
                    rationale="Every required capability is registered for the selected pack.",
                )
            )
        return tuple(evidence)

    def _provisional_fallback(
        self,
        domains: tuple[DomainPackManifest, ...],
        tasks: tuple[TaskPackManifest, ...],
        domain_registry: DomainPackRegistry,
        task_registry: TaskPackRegistry,
        available: Set[str],
    ) -> PackSelection:
        generic_domain = domain_registry.require("generic")
        generic_task = task_registry.require("generic_data_integration")
        generic_required = self._required_capabilities((generic_domain,), (generic_task,))
        missing = tuple(sorted(generic_required - available))
        proposed_domains = tuple(domain_registry.reference(pack) for pack in domains)
        proposed_tasks = tuple(task_registry.reference(pack) for pack in tasks)
        if missing:
            return PackSelection(
                mode=RoutingMode.UNSUPPORTED,
                proposed_domain_packs=proposed_domains,
                proposed_task_packs=proposed_tasks,
                missing_capabilities=missing,
                fallback_path=("register_missing_capabilities", "human_review"),
                evidence=self._missing_evidence(missing),
            )
        fallback = make_evidence(
            kind=EvidenceKind.FALLBACK,
            source="router",
            target="generic_data_integration",
            signal="low_routing_confidence",
            weight=0.0,
            rationale="Specialist candidates remain proposed until evidence is confirmed.",
        )
        return PackSelection(
            mode=RoutingMode.PROVISIONAL,
            domain_packs=(domain_registry.reference(generic_domain),),
            task_packs=(task_registry.reference(generic_task),),
            proposed_domain_packs=proposed_domains,
            proposed_task_packs=proposed_tasks,
            fallback_path=("generic_domain", "generic_data_integration", "human_review"),
            evidence=(fallback,),
        )

    def _capability_fallback(
        self,
        domains: tuple[DomainPackManifest, ...],
        tasks: tuple[TaskPackManifest, ...],
        missing: tuple[str, ...],
        domain_registry: DomainPackRegistry,
        task_registry: TaskPackRegistry,
        available: Set[str],
    ) -> PackSelection:
        generic_domain = domain_registry.require("generic")
        generic_task = task_registry.require("generic_data_integration")
        generic_required = self._required_capabilities((generic_domain,), (generic_task,))
        proposed_domains = tuple(domain_registry.reference(pack) for pack in domains)
        proposed_tasks = tuple(task_registry.reference(pack) for pack in tasks)
        evidence = self._missing_evidence(missing)
        if generic_required <= available:
            fallback = make_evidence(
                kind=EvidenceKind.FALLBACK,
                source="router",
                target="generic_data_integration",
                signal="specialist_capability_missing",
                weight=0.0,
                rationale="The conservative generic path is enabled instead of an unusable pack.",
            )
            return PackSelection(
                mode=RoutingMode.GENERIC,
                domain_packs=(domain_registry.reference(generic_domain),),
                task_packs=(task_registry.reference(generic_task),),
                proposed_domain_packs=proposed_domains,
                proposed_task_packs=proposed_tasks,
                missing_capabilities=missing,
                fallback_path=("generic_domain", "generic_data_integration", "human_review"),
                evidence=unique_evidence((*evidence, fallback)),
            )
        all_missing = tuple(sorted(set(missing) | (generic_required - available)))
        return PackSelection(
            mode=RoutingMode.UNSUPPORTED,
            proposed_domain_packs=proposed_domains,
            proposed_task_packs=proposed_tasks,
            missing_capabilities=all_missing,
            fallback_path=("register_missing_capabilities", "human_review"),
            evidence=self._missing_evidence(all_missing),
        )

    @staticmethod
    def _missing_evidence(missing: tuple[str, ...]) -> tuple[RoutingEvidence, ...]:
        return tuple(
            make_evidence(
                kind=EvidenceKind.CAPABILITY,
                source="capability_registry",
                target=capability,
                signal="required_capability_missing",
                weight=0.0,
                rationale="A required capability is absent or unhealthy; the pack is not enabled.",
            )
            for capability in missing
        )


class UnsupportedTaskDetector:
    """Expose unsupported detection as a typed, independently testable policy."""

    def is_unsupported(self, selection: PackSelection) -> bool:
        """Return true only when no conservative pack can safely run."""

        return selection.mode == RoutingMode.UNSUPPORTED


class RoutingCalibrator:
    """Combine domain and archetype confidence conservatively."""

    def combined_confidence(
        self,
        domain_profile: DomainProfile,
        task_archetypes: TaskArchetypeSet,
    ) -> float:
        """Use the weakest required classifier as the route confidence."""

        return round(min(domain_profile.confidence, task_archetypes.confidence), 6)
