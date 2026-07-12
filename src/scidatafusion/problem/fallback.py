"""Deterministic, offline candidate extraction for M01."""

from __future__ import annotations

import hashlib
import re

from scidatafusion.contracts.problem import (
    CandidateBatch,
    ConditionIntent,
    ConditionKind,
    EntityIntent,
    ExtractionMethod,
    OutputFormat,
    OutputPreference,
    ProblemUnit,
    ScopeDimension,
    ScopeIntent,
    SourceSpan,
    VariableIntent,
)

_RESEARCH_MARKER = re.compile(
    r"研究|分析|比较|探索|测量|调查|提取|study|analy[sz]e|compare|investigate|measure|research",
    re.IGNORECASE,
)
_VARIABLE = re.compile(
    r"光变曲线|溶解氧|pH\s*值?|光谱|红移|亮度|温度|质量|通量|速度|丰度|浓度|"
    r"light\s+curves?|dissolved\s+oxygen|spectra?|redshifts?|luminosit(?:y|ies)|"
    r"temperatures?|masses?|flux(?:es)?|velocit(?:y|ies)|abundances?|concentrations?",
    re.IGNORECASE,
)
_CLAUSE = re.compile(
    r"[^?\uff1f;\uff1b.!\u3002\n]+[?\uff1f;\uff1b.!\u3002]?",
    re.MULTILINE,
)
_COMPARISON = re.compile(
    r"(?:[A-Za-z_][\w -]{0,32}|[\u4e00-\u9fff]{1,16})\s*"
    r"(?:<=|>=|=|<|>|≤|≥)\s*[+-]?\d+(?:\.\d+)?(?:\s*[A-Za-z/%°\u4e00-\u9fff]+)?"
)
_EXCLUSION = re.compile(
    r"(?:不包括|排除|剔除|without|excluding|exclude)\s*"
    r"[^,\uff0c;\uff1b.\u3002?\uff1f]+",
    re.IGNORECASE,
)
_CONDITION = re.compile(
    r"(?:在|仅限于|under|where|subject\s+to)\s+"
    r"[^,\uff0c;\uff1b.\u3002?\uff1f]+",
    re.IGNORECASE,
)
_TEMPORAL = re.compile(
    r"(?:19|20)\d{2}(?:\s*(?:-|至|到|through|to)\s*(?:19|20)\d{2})?\s*年?|"
    r"最近\s*\d*\s*年|recent|last\s+\d+\s+(?:days?|months?|years?)",
    re.IGNORECASE,
)
_SPATIAL = re.compile(
    r"(?:RA|Dec|latitude|longitude|赤经|赤纬|纬度|经度)\s*[:=]?\s*"
    r"[^,\uff0c;\uff1b.\u3002?\uff1f]+",
    re.IGNORECASE,
)
_OUTPUT = re.compile(r"\b(?:csv|parquet|json|notebook)\b", re.IGNORECASE)
_LEADING_DESIRE = re.compile(
    r"^(?:请帮我|帮我|我希望|我想|希望|please|I\s+(?:want|would\s+like)\s+to)\s*",
    re.IGNORECASE,
)


def _source_span(text: str, start: int, end: int) -> SourceSpan:
    return SourceSpan(start=start, end=end, text=text[start:end])


def _trim_bounds(text: str, start: int, end: int, *, punctuation: bool = False) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if punctuation:
        while end > start and text[end - 1] in "?\uff1f;\uff1b.!\u3002":
            end -= 1
        while end > start and text[end - 1].isspace():
            end -= 1
    return start, end


def _unit_id(question: str, start: int) -> str:
    digest = hashlib.sha256(f"{start}:{question}".encode()).hexdigest()[:16]
    return f"unit_{digest}"


def _problem_units(text: str) -> tuple[ProblemUnit, ...]:
    clauses: list[tuple[int, int]] = []
    all_clauses: list[tuple[int, int]] = []
    for match in _CLAUSE.finditer(text):
        start, end = _trim_bounds(text, match.start(), match.end(), punctuation=True)
        if start == end:
            continue
        all_clauses.append((start, end))
        if _RESEARCH_MARKER.search(text[start:end]):
            clauses.append((start, end))
    if not clauses:
        clauses = all_clauses[:1]
    if not clauses:
        start, end = _trim_bounds(text, 0, len(text), punctuation=True)
        if start == end:
            start, end = _trim_bounds(text, 0, len(text))
        clauses = [(start, end)]

    units = []
    for start, end in clauses:
        span = _source_span(text, start, end)
        units.append(
            ProblemUnit(
                unit_id=_unit_id(span.text, start),
                question=span.text,
                confidence=1.0,
                evidence=(span,),
                method=ExtractionMethod.DETERMINISTIC_RULE,
                basis="Exact research clause retained from the accepted task input.",
            )
        )
    return tuple(units)


def _variable_intents(text: str) -> tuple[VariableIntent, ...]:
    results: list[VariableIntent] = []
    seen: set[tuple[int, int]] = set()
    for match in _VARIABLE.finditer(text):
        key = (match.start(), match.end())
        if key in seen:
            continue
        seen.add(key)
        span = _source_span(text, *key)
        results.append(
            VariableIntent(
                name=span.text,
                confidence=0.98,
                evidence=(span,),
                method=ExtractionMethod.DETERMINISTIC_RULE,
                basis="Matched an explicit scientific variable or data-product phrase.",
            )
        )
    return tuple(results)


def _entity_intents(text: str, variables: tuple[VariableIntent, ...]) -> tuple[EntityIntent, ...]:
    results: list[EntityIntent] = []
    seen: set[str] = set()
    variable_starts = sorted(span.start for item in variables for span in item.evidence)

    for unit in _problem_units(text):
        unit_span = unit.evidence[0]
        first_variable = next(
            (
                position
                for position in variable_starts
                if unit_span.start < position < unit_span.end
            ),
            None,
        )
        local_text = text[unit_span.start : unit_span.end]
        markers = list(_RESEARCH_MARKER.finditer(local_text))
        if not markers and first_variable is None:
            continue
        start = unit_span.start
        if markers:
            eligible = [
                marker
                for marker in markers
                if first_variable is None or unit_span.start + marker.end() <= first_variable
            ]
            if eligible:
                start = unit_span.start + eligible[-1].end()
        else:
            desire = _LEADING_DESIRE.match(local_text)
            if desire is not None:
                start = unit_span.start + desire.end()

        end = first_variable if first_variable is not None else unit_span.end
        start, end = _trim_bounds(text, start, end)
        while end > start and text[end - 1] in "的:'\"":
            end -= 1
            start, end = _trim_bounds(text, start, end)
        if start == end:
            continue
        candidate = text[start:end]
        candidate = re.split(
            r"\s+(?:under|where|within|from|between)\s+|(?:在|仅限于)",
            candidate,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].rstrip()
        end = start + len(candidate)
        if not candidate or len(candidate) > 200:
            continue
        normalized = candidate.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        span = _source_span(text, start, end)
        results.append(
            EntityIntent(
                name=span.text,
                entity_type=None,
                confidence=0.9 if first_variable is not None else 0.75,
                evidence=(span,),
                method=ExtractionMethod.DETERMINISTIC_RULE,
                basis="Text between the research verb and requested variable was retained verbatim.",
            )
        )
    return tuple(results)


def _condition_intents(text: str) -> tuple[ConditionIntent, ...]:
    matches: list[tuple[int, int, ConditionKind, bool]] = []
    matches.extend(
        (match.start(), match.end(), ConditionKind.FILTER, False)
        for match in _COMPARISON.finditer(text)
    )
    matches.extend(
        (match.start(), match.end(), ConditionKind.EXCLUSION, True)
        for match in _EXCLUSION.finditer(text)
    )
    matches.extend(
        (match.start(), match.end(), ConditionKind.OBSERVATION, False)
        for match in _CONDITION.finditer(text)
    )
    results: list[ConditionIntent] = []
    for start, end, kind, negated in sorted(set(matches)):
        start, end = _trim_bounds(text, start, end)
        span = _source_span(text, start, end)
        results.append(
            ConditionIntent(
                expression=span.text,
                kind=kind,
                negated=negated,
                confidence=0.9,
                evidence=(span,),
                method=ExtractionMethod.DETERMINISTIC_RULE,
                basis="Explicit condition retained verbatim; no value conversion was performed.",
            )
        )
    return tuple(results)


def _scope(text: str, pattern: re.Pattern[str], dimension: ScopeDimension) -> ScopeIntent | None:
    match = pattern.search(text)
    if match is None:
        return None
    start, end = _trim_bounds(text, match.start(), match.end())
    span = _source_span(text, start, end)
    return ScopeIntent(
        dimension=dimension,
        expression=span.text,
        confidence=0.9,
        evidence=(span,),
        method=ExtractionMethod.DETERMINISTIC_RULE,
        basis="Explicit scope text retained without normalization or inferred values.",
    )


def _output_preferences(text: str) -> tuple[OutputPreference, ...]:
    results = []
    for match in _OUTPUT.finditer(text):
        span = _source_span(text, match.start(), match.end())
        results.append(
            OutputPreference(
                format=OutputFormat(match.group().lower()),
                confidence=1.0,
                evidence=(span,),
                method=ExtractionMethod.USER_EXPLICIT,
                basis="Output format was explicitly named by the user.",
            )
        )
    return tuple(results)


class DeterministicCandidateExtractor:
    """Extract conservative candidates offline using only exact source spans."""

    async def extract(self, text: str) -> CandidateBatch:
        """Return deterministic candidates without network or model calls."""

        variables = _variable_intents(text)
        return CandidateBatch(
            problem_units=_problem_units(text),
            entities=_entity_intents(text, variables),
            variables=variables,
            conditions=_condition_intents(text),
            temporal_scope=_scope(text, _TEMPORAL, ScopeDimension.TEMPORAL),
            spatial_scope=_scope(text, _SPATIAL, ScopeDimension.SPATIAL),
            output_preferences=_output_preferences(text),
        )
