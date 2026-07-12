"""Evidence-backed deterministic domain routing."""

from __future__ import annotations

from typing import Protocol

from scidatafusion.contracts.routing import (
    DomainProfile,
    EvidenceKind,
    RankedDomain,
    RoutingEvidence,
)
from scidatafusion.domain.registry import DomainPackRegistry
from scidatafusion.routing._rules import (
    calibrated_confidence,
    find_rule_matches,
    make_evidence,
    unique_evidence,
)

_CONFIDENCE_THRESHOLD = 0.65
_SECONDARY_MIN_SCORE = 0.7
_SECONDARY_RELATIVE_SCORE = 0.45


class DomainRouter(Protocol):
    """Stable interface for replaceable domain candidate generation."""

    def route(
        self,
        text: str,
        registry: DomainPackRegistry,
        *,
        prior_evidence: tuple[RoutingEvidence, ...] = (),
    ) -> DomainProfile:
        """Return an ordered domain profile supported by source evidence."""


class RuleBasedDomainRouter:
    """Vote over versioned manifest rules and retain every matching span."""

    def route(
        self,
        text: str,
        registry: DomainPackRegistry,
        *,
        prior_evidence: tuple[RoutingEvidence, ...] = (),
    ) -> DomainProfile:
        """Classify text without model self-scores or mutable global state."""

        scored: list[tuple[str, float, tuple[RoutingEvidence, ...]]] = []
        for pack in registry.packs:
            if pack.name == "generic":
                continue
            votes: list[RoutingEvidence] = []
            score = 0.0
            for rule in pack.keyword_rules:
                matches = find_rule_matches(text, rule)
                if not matches:
                    continue
                score += rule.weight
                start, end, matched_text = matches[0]
                votes.append(
                    make_evidence(
                        kind=EvidenceKind.KEYWORD,
                        source="domain_registry",
                        target=pack.name,
                        signal=rule.term,
                        weight=rule.weight,
                        matched_text=matched_text,
                        start=start,
                        end=end,
                        rationale=f"Matched a versioned keyword rule from domain pack {pack.name}.",
                    )
                )
            if score > 0.0:
                scored.append((pack.name, round(score, 6), tuple(votes)))

        scored.sort(key=lambda item: (-item[1], item[0]))
        if not scored:
            fallback = make_evidence(
                kind=EvidenceKind.FALLBACK,
                source="router",
                target="generic",
                signal="no_domain_threshold_match",
                weight=0.0,
                rationale="No registered specialist domain has enough deterministic evidence.",
            )
            return DomainProfile(
                primary_domain="generic",
                confidence=0.25,
                provisional=True,
                ranked_candidates=(
                    RankedDomain(
                        domain="generic",
                        score=0.0,
                        confidence=0.25,
                        evidence_count=1,
                    ),
                ),
                evidence=unique_evidence((*prior_evidence, fallback)),
            )

        ranked = tuple(
            RankedDomain(
                domain=name,
                score=score,
                confidence=calibrated_confidence(score, len(votes)),
                evidence_count=len(votes),
            )
            for name, score, votes in scored
        )
        primary = ranked[0]
        secondary = tuple(
            candidate.domain
            for candidate in ranked[1:]
            if candidate.score >= _SECONDARY_MIN_SCORE
            and candidate.score >= primary.score * _SECONDARY_RELATIVE_SCORE
        )
        selected_names = (primary.domain, *secondary)
        selected_packs = tuple(registry.require(name) for name in selected_names)
        subdomains = tuple(
            dict.fromkeys(subdomain for pack in selected_packs for subdomain in pack.subdomains)
        )
        selected_evidence = tuple(
            evidence for name, _, votes in scored if name in selected_names for evidence in votes
        )
        return DomainProfile(
            primary_domain=primary.domain,
            secondary_domains=secondary,
            subdomains=subdomains,
            confidence=primary.confidence,
            provisional=primary.confidence < _CONFIDENCE_THRESHOLD,
            ranked_candidates=ranked,
            evidence=unique_evidence((*prior_evidence, *selected_evidence)),
        )
