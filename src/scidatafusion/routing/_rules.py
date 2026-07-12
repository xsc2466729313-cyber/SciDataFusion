"""Pure helpers shared by deterministic routing stages."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable

from scidatafusion.contracts.routing import EvidenceKind, RoutingEvidence
from scidatafusion.domain.registry import KeywordRule, canonical_hash

_DIRECTIVE_CUES = (
    re.compile(
        r"\b(?:ignore|disregard|override)\b.{0,200}"
        r"\b(?:route|routing|domain|pack|classif(?:y|ication))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:route|classify|select|enable)\b.{0,100}\b(?:domain|pack)\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?:\u5ffd\u7565|\u65e0\u89c6).{0,100}(?:\u8def\u7531|\u9886\u57df|\u5305)"),
    re.compile(r"(?:\u8def\u7531\u5230|\u9009\u62e9|\u542f\u7528).{0,60}(?:\u9886\u57df|\u5305)"),
)
_SEGMENT_PATTERN = re.compile(r"[^.!?\n\u3002\uff01\uff1f;\uff1b]+[.!?\u3002\uff01\uff1f;\uff1b]?")


def normalize_text(value: str) -> str:
    """Normalize Unicode and case without interpreting document instructions."""

    return unicodedata.normalize("NFKC", value).casefold()


def make_evidence(
    *,
    kind: EvidenceKind,
    source: str,
    target: str,
    signal: str,
    weight: float,
    rationale: str,
    matched_text: str | None = None,
    start: int | None = None,
    end: int | None = None,
) -> RoutingEvidence:
    """Create an evidence atom whose identifier is derived from its content."""

    payload = {
        "end": end,
        "kind": kind.value,
        "matched_text": matched_text,
        "rationale": rationale,
        "signal": signal,
        "source": source,
        "start": start,
        "target": target,
        "weight": weight,
    }
    evidence_id = f"rte_{canonical_hash(payload)[:32]}"
    return RoutingEvidence(
        evidence_id=evidence_id,
        kind=kind,
        source=source,  # type: ignore[arg-type]
        target=target,
        signal=signal,
        weight=weight,
        matched_text=matched_text,
        start=start,
        end=end,
        rationale=rationale,
    )


def mask_routing_directives(text: str) -> tuple[str, tuple[RoutingEvidence, ...]]:
    """Mask prompt-like routing commands while preserving offsets for evidence."""

    masked = list(text)
    evidence: list[RoutingEvidence] = []
    for segment_match in _SEGMENT_PATTERN.finditer(text):
        segment = segment_match.group(0)
        if not any(pattern.search(segment) for pattern in _DIRECTIVE_CUES):
            continue
        start, end = segment_match.span()
        visible = segment.strip()
        visible_start = start + len(segment) - len(segment.lstrip())
        visible_end = visible_start + len(visible)
        evidence.append(
            make_evidence(
                kind=EvidenceKind.SAFETY_FILTER,
                source="research_goal",
                target="routing_input",
                signal="routing_directive_masked",
                weight=0.0,
                matched_text=visible[:256],
                start=visible_start,
                end=min(visible_end, visible_start + 256),
                rationale="Routing commands in user or document text are data, not classifier rules.",
            )
        )
        for position in range(start, end):
            if masked[position] not in ".!?\n\u3002\uff01\uff1f;\uff1b":
                masked[position] = " "
    return "".join(masked), tuple(evidence)


def find_rule_matches(text: str, rule: KeywordRule) -> tuple[tuple[int, int, str], ...]:
    """Find non-overlapping phrase matches with word boundaries for Latin terms."""

    normalized = normalize_text(text)
    term = normalize_text(rule.term)
    prefix = r"(?<![\w])" if term[0].isascii() and term[0].isalnum() else ""
    suffix = r"(?![\w])" if term[-1].isascii() and term[-1].isalnum() else ""
    pattern = re.compile(f"{prefix}{re.escape(term)}{suffix}")
    return tuple(
        (match.start(), match.end(), match.group(0)) for match in pattern.finditer(normalized)
    )


def calibrated_confidence(score: float, evidence_count: int) -> float:
    """Map deterministic rule votes to a bounded, conservative confidence."""

    return round(min(0.99, 0.2 + score * 0.22 + min(evidence_count, 3) * 0.08), 6)


def unique_evidence(items: Iterable[RoutingEvidence]) -> tuple[RoutingEvidence, ...]:
    """Deduplicate evidence by content-derived identifier while retaining order."""

    seen: set[str] = set()
    result: list[RoutingEvidence] = []
    for item in items:
        if item.evidence_id not in seen:
            seen.add(item.evidence_id)
            result.append(item)
    return tuple(result)


def deterministic_hex(*parts: str) -> str:
    """Hash textual replay-key parts without depending on process hash randomization."""

    digest = hashlib.sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()
