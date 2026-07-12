"""Deterministic, registry-grounded query expansion for M04."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from scidatafusion.contracts.scientific import ResearchConcept
from scidatafusion.contracts.search import (
    LanguageCode,
    LocalizedQueryHint,
    SearchTerm,
    SourceCapabilityRegistry,
)

_WHITESPACE = re.compile(r"\s+")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def normalize_query(value: str) -> str:
    """Return the stable NFKC/case-folded key used for query replay and deduplication."""

    normalized = unicodedata.normalize("NFKC", value)
    return _WHITESPACE.sub(" ", normalized).strip().casefold()


def deduplicate_queries(values: Iterable[str]) -> tuple[str, ...]:
    """Keep the first display form for each normalized query, preserving input order."""

    unique: dict[str, str] = {}
    for value in values:
        display = _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value)).strip()
        key = normalize_query(display)
        if key:
            unique.setdefault(key, display)
    return tuple(unique.values())


def infer_language(value: str) -> LanguageCode:
    """Classify only the language distinction needed by the registered query adapters."""

    return "zh" if _CJK.search(value) is not None else "en"


@dataclass(frozen=True, slots=True)
class ExpandedQuery:
    """One language-specific candidate before connector capability binding."""

    language: LanguageCode
    text: str
    terms: tuple[str, ...]


class QueryExpander:
    """Expand evidence-grounded concepts only with content-addressed registry terms."""

    def expand(
        self,
        concepts: tuple[ResearchConcept, ...],
        domains: tuple[str, ...],
        registry: SourceCapabilityRegistry,
        hints: tuple[LocalizedQueryHint, ...] = (),
    ) -> tuple[ExpandedQuery, ...]:
        """Build stable multilingual candidates without calling a model or the network."""

        by_language: dict[LanguageCode, list[str]] = {}
        matched_concepts: set[str] = set()
        normalized_domains = {item.casefold() for item in domains}
        normalized_concepts = {
            concept.concept_id: normalize_query(concept.term) for concept in concepts
        }
        for expansion in registry.term_expansions:
            if not normalized_domains.intersection(item.casefold() for item in expansion.domains):
                continue
            expansion_keys = tuple(normalize_query(item.term) for item in expansion.terms)
            matching_ids = {
                concept_id
                for concept_id, concept_key in normalized_concepts.items()
                if any(
                    expansion_key in concept_key or concept_key in expansion_key
                    for expansion_key in expansion_keys
                )
            }
            if not matching_ids:
                continue
            matched_concepts.update(matching_ids)
            for term in expansion.terms:
                by_language.setdefault(term.language, []).append(term.term)

        for concept in concepts:
            if concept.concept_id in matched_concepts:
                continue
            language = infer_language(concept.term)
            by_language.setdefault(language, []).append(concept.term)

        hints_by_language: dict[LanguageCode, list[str]] = {}
        for hint in hints:
            hints_by_language.setdefault(hint.language, []).append(hint.text)

        expanded: list[ExpandedQuery] = []
        for language in sorted(by_language):
            terms = deduplicate_queries(by_language[language])
            language_hints = (
                *hints_by_language.get(language, ()),
                *hints_by_language.get("und", ()),
            )
            query_parts = deduplicate_queries((*terms, *language_hints))
            text = " ".join(query_parts)
            if text:
                expanded.append(ExpandedQuery(language=language, text=text, terms=terms))
        return tuple(expanded)


def terms_parameter(query: ExpandedQuery) -> tuple[SearchTerm, ...]:
    """Expose expanded terms in the same typed representation used by the registry."""

    return tuple(SearchTerm(language=query.language, term=term) for term in query.terms)
