"""Deterministic multi-label task-archetype routing."""

from __future__ import annotations

from typing import Protocol

from scidatafusion.contracts.routing import (
    EvidenceKind,
    RankedArchetype,
    RoutingEvidence,
    TaskArchetypeSet,
)
from scidatafusion.domain.registry import TaskPackRegistry
from scidatafusion.routing._rules import (
    calibrated_confidence,
    find_rule_matches,
    make_evidence,
    unique_evidence,
)

_CONFIDENCE_THRESHOLD = 0.62
_SELECTION_MIN_SCORE = 0.65


class ArchetypeRouter(Protocol):
    """Stable interface for replaceable task-archetype candidate generation."""

    def route(
        self,
        text: str,
        registry: TaskPackRegistry,
        *,
        prior_evidence: tuple[RoutingEvidence, ...] = (),
    ) -> TaskArchetypeSet:
        """Return an ordered multi-label archetype set."""


class RuleBasedArchetypeRouter:
    """Route with manifest phrase votes and declared archetype relationships."""

    def route(
        self,
        text: str,
        registry: TaskPackRegistry,
        *,
        prior_evidence: tuple[RoutingEvidence, ...] = (),
    ) -> TaskArchetypeSet:
        """Classify task shape and record every deterministic vote."""

        scores: dict[str, float] = {}
        evidence_by_name: dict[str, list[RoutingEvidence]] = {}
        for pack in registry.packs:
            if pack.name == "generic_data_integration":
                continue
            for rule in pack.keyword_rules:
                matches = find_rule_matches(text, rule)
                if not matches:
                    continue
                scores[pack.name] = scores.get(pack.name, 0.0) + rule.weight
                start, end, matched_text = matches[0]
                evidence_by_name.setdefault(pack.name, []).append(
                    make_evidence(
                        kind=EvidenceKind.KEYWORD,
                        source="task_registry",
                        target=pack.name,
                        signal=rule.term,
                        weight=rule.weight,
                        matched_text=matched_text,
                        start=start,
                        end=end,
                        rationale=f"Matched a versioned keyword rule from task pack {pack.name}.",
                    )
                )

        direct_names = frozenset(scores)
        for pack in registry.packs:
            triggers = tuple(name for name in pack.activate_with_any if name in direct_names)
            if not triggers or pack.name in scores:
                continue
            trigger_score = max(scores[name] for name in triggers)
            relationship_score = round(max(0.75, trigger_score * 0.45), 6)
            scores[pack.name] = relationship_score
            evidence_by_name.setdefault(pack.name, []).append(
                make_evidence(
                    kind=EvidenceKind.RELATIONSHIP,
                    source="task_registry",
                    target=pack.name,
                    signal=f"activated_by:{','.join(sorted(triggers))}",
                    weight=relationship_score,
                    rationale="The task registry declares this companion archetype dependency.",
                )
            )

        if not scores:
            fallback = make_evidence(
                kind=EvidenceKind.FALLBACK,
                source="router",
                target="generic_data_integration",
                signal="no_archetype_threshold_match",
                weight=0.0,
                rationale="No registered specialist archetype has enough deterministic evidence.",
            )
            return TaskArchetypeSet(
                primary_archetype="generic_data_integration",
                archetypes=("generic_data_integration",),
                confidence=0.25,
                provisional=True,
                ranked_candidates=(
                    RankedArchetype(
                        archetype="generic_data_integration",
                        score=0.0,
                        confidence=0.25,
                        evidence_count=1,
                    ),
                ),
                evidence=unique_evidence((*prior_evidence, fallback)),
            )

        ordered = sorted(scores, key=lambda name: (-scores[name], name))
        ranked = tuple(
            RankedArchetype(
                archetype=name,
                score=round(scores[name], 6),
                confidence=calibrated_confidence(scores[name], len(evidence_by_name[name])),
                evidence_count=len(evidence_by_name[name]),
            )
            for name in ordered
        )
        selected = tuple(item.archetype for item in ranked if item.score >= _SELECTION_MIN_SCORE)
        primary = ranked[0]
        selected_evidence = tuple(
            evidence for name in selected for evidence in evidence_by_name[name]
        )
        return TaskArchetypeSet(
            primary_archetype=primary.archetype,
            archetypes=selected,
            confidence=primary.confidence,
            provisional=primary.confidence < _CONFIDENCE_THRESHOLD,
            ranked_candidates=ranked,
            evidence=unique_evidence((*prior_evidence, *selected_evidence)),
        )
