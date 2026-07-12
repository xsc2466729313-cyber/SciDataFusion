"""Evidence-preserving normalization and deduplication of Connector records."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from scidatafusion.contracts.connectors import (
    CandidateCoverageClaim,
    CandidateIdentifier,
    CandidateObservation,
    ConnectorRecord,
    CoverageAssessment,
    CoverageBasis,
    IdentifierKind,
    MetadataConflict,
    SearchEvidence,
    SourceCandidate,
)
from scidatafusion.contracts.search import ExecutableQuery
from scidatafusion.domain.registry import canonical_hash

from .integrity import calculate_source_candidate_hash
from .scoring import (
    assess_source,
    normalize_file_format,
    normalize_license_label,
)

_SPACE = re.compile(r"\s+")
_DOI = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)
_DOI_PREFIX = re.compile(r"^doi\s*:\s*", re.IGNORECASE)
_TRACKING_PARAMETERS = frozenset(
    {
        "dclid",
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "msclkid",
        "yclid",
    }
)


@dataclass(frozen=True, slots=True)
class ObservedRecord:
    """One strict Connector record bound to its query, observation, and raw evidence."""

    record: ConnectorRecord
    query: ExecutableQuery
    observation: CandidateObservation
    evidence: tuple[SearchEvidence, ...]

    def __post_init__(self) -> None:
        if not self.evidence:
            raise ValueError("an observed record requires at least one SearchEvidence item")
        if (
            self.observation.query_id != self.query.query_id
            or self.observation.source_id != self.query.source_id
            or self.observation.category is not self.query.category
            or self.observation.external_record_id != self.record.external_record_id
        ):
            raise ValueError("observation metadata must match the record and executable query")
        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("observed record evidence ids must be unique")
        if set(evidence_ids) != set(self.observation.evidence_ids):
            raise ValueError("observation evidence ids must exactly match supplied evidence")
        if any(
            item.query_id != self.query.query_id
            or item.source_id != self.query.source_id
            or item.connector_id != self.observation.connector_id
            or item.raw_response_hash != self.observation.raw_response_hash
            or item.record_hash != self.record.record_hash
            for item in self.evidence
        ):
            raise ValueError("SearchEvidence provenance must match the observed record")


def normalize_doi(value: str | None) -> str | None:
    """Normalize a DOI label or DOI resolver URL, returning ``None`` when invalid."""

    if value is None:
        return None
    normalized = unicodedata.normalize("NFKC", value).strip()
    normalized = _DOI_PREFIX.sub("", normalized)
    parsed = urlsplit(normalized)
    if parsed.scheme or parsed.netloc:
        if (
            parsed.scheme.casefold() not in {"http", "https"}
            or parsed.hostname is None
            or parsed.hostname.casefold().rstrip(".") not in {"doi.org", "dx.doi.org"}
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        normalized = unquote(parsed.path).lstrip("/")
    normalized = normalized.strip().casefold()
    if not _DOI.fullmatch(normalized) or any(character.isspace() for character in normalized):
        return None
    return normalized


def normalize_https_url(value: str | None) -> str | None:
    """Canonicalize a safe HTTPS locator without following or resolving it."""

    if value is None:
        return None
    try:
        parsed = urlsplit(unicodedata.normalize("NFKC", value).strip())
        if (
            parsed.scheme.casefold() != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        host = parsed.hostname.rstrip(".").casefold()
        if not host:
            return None
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return None
        port = parsed.port
    except ValueError:
        return None
    if port not in {None, 443}:
        return None

    rendered_host = f"[{host}]" if ":" in host else host
    netloc = rendered_host
    retained_query = sorted(
        (
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if key.casefold() not in _TRACKING_PARAMETERS and not key.casefold().startswith("utm_")
        ),
        key=lambda pair: (pair[0].casefold(), pair[0], pair[1]),
    )
    return urlunsplit(
        (
            "https",
            netloc,
            parsed.path or "/",
            urlencode(retained_query, doseq=True),
            "",
        )
    )


def normalize_title(value: str) -> str:
    """Return the stable display form of a title without interpreting its content."""

    return _SPACE.sub(" ", unicodedata.normalize("NFKC", value)).strip()


def normalize_title_key(value: str) -> str:
    """Return an NFKC, case-folded title key with punctuation treated as spacing."""

    normalized = normalize_title(value).casefold()
    without_punctuation = "".join(
        " " if unicodedata.category(character).startswith("P") else character
        for character in normalized
    )
    return _SPACE.sub(" ", without_punctuation).strip()


def record_dedup_key(record: ConnectorRecord) -> str:
    """Choose the strongest deterministic key: DOI, URL, then normalized title and year."""

    return _record_aliases(record)[0]


def _title_year_key(record: ConnectorRecord) -> str:
    title_key = normalize_title_key(record.title)
    title_hash = hashlib.sha256(title_key.encode("utf-8")).hexdigest()
    year = str(record.published_date.year) if record.published_date is not None else "unknown"
    return f"title:{title_hash}|year:{year}"


def _record_aliases(record: ConnectorRecord) -> tuple[str, ...]:
    aliases = {_title_year_key(record)}
    aliases.update(_record_strong_aliases(record))
    return tuple(sorted(aliases, key=_alias_sort_key))


def _record_strong_aliases(record: ConnectorRecord) -> tuple[str, ...]:
    aliases: set[str] = set()
    doi = normalize_doi(record.doi)
    if doi is not None:
        aliases.add(f"doi:{doi}")
    url = normalize_https_url(record.landing_url)
    if url is not None:
        aliases.add(f"url:{url}")
    return tuple(sorted(aliases, key=_alias_sort_key))


def _alias_sort_key(value: str) -> tuple[int, str]:
    prefix = value.partition(":")[0]
    return ({"doi": 0, "url": 1, "title": 2}[prefix], value)


def normalize_candidates(records: Iterable[ObservedRecord]) -> tuple[SourceCandidate, ...]:
    """Normalize, deduplicate, and deterministically sort evidence-linked source hits."""

    ordered = tuple(sorted(records, key=_observed_record_sort_key))
    parents = list(range(len(ordered)))
    component_dois = [
        {doi} if (doi := normalize_doi(item.record.doi)) is not None else set() for item in ordered
    ]
    strong_alias_owners: dict[str, int] = {}
    for index, item in enumerate(ordered):
        for alias in _record_strong_aliases(item.record):
            owner = strong_alias_owners.setdefault(alias, index)
            _union_without_doi_conflict(parents, component_dois, index, owner)

    title_groups: dict[str, list[int]] = defaultdict(list)
    for index, item in enumerate(ordered):
        title_groups[_title_year_key(item.record)].append(index)
    for indices in title_groups.values():
        roots = {_find(parents, index) for index in indices}
        strong_roots = {
            _find(parents, index)
            for index in indices
            if _record_strong_aliases(ordered[index].record)
        }
        if len(strong_roots) > 1:
            continue
        owner = min(strong_roots or roots)
        for index in indices:
            _union_without_doi_conflict(parents, component_dois, owner, index)

    components: dict[int, list[ObservedRecord]] = defaultdict(list)
    for index, item in enumerate(ordered):
        components[_find(parents, index)].append(item)
    canonical_groups = tuple((_canonical_group_key(items), items) for items in components.values())
    candidates = (
        _build_candidate(key, items)
        for key, items in sorted(canonical_groups, key=lambda item: item[0])
    )
    return tuple(sorted(candidates, key=lambda item: (item.dedup_key, item.candidate_id)))


def _find(parents: list[int], index: int) -> int:
    while parents[index] != index:
        parents[index] = parents[parents[index]]
        index = parents[index]
    return index


def _union(parents: list[int], left: int, right: int) -> None:
    left_root = _find(parents, left)
    right_root = _find(parents, right)
    if left_root == right_root:
        return
    if left_root > right_root:
        left_root, right_root = right_root, left_root
    parents[right_root] = left_root


def _union_without_doi_conflict(
    parents: list[int],
    component_dois: list[set[str]],
    left: int,
    right: int,
) -> bool:
    left_root = _find(parents, left)
    right_root = _find(parents, right)
    if left_root == right_root:
        return True
    left_dois = component_dois[left_root]
    right_dois = component_dois[right_root]
    if left_dois and right_dois and left_dois.isdisjoint(right_dois):
        return False
    if left_root > right_root:
        left_root, right_root = right_root, left_root
    parents[right_root] = left_root
    component_dois[left_root] = left_dois | right_dois
    component_dois[right_root] = set()
    return True


def _canonical_group_key(items: Sequence[ObservedRecord]) -> str:
    aliases = {alias for item in items for alias in _record_aliases(item.record)}
    return min(aliases, key=_alias_sort_key)


def _observed_record_sort_key(item: ObservedRecord) -> tuple[object, ...]:
    return (
        record_dedup_key(item.record),
        item.observation.query_id,
        item.observation.source_id,
        item.observation.connector_id,
        item.observation.rank,
        item.record.external_record_id,
        item.record.record_hash,
        tuple(sorted(item.observation.evidence_ids)),
    )


def _build_candidate(dedup_key: str, items: Sequence[ObservedRecord]) -> SourceCandidate:
    ordered = tuple(sorted(items, key=_observed_record_sort_key))
    dois = _sorted_text(
        doi for item in ordered if (doi := normalize_doi(item.record.doi)) is not None
    )
    landing_urls = _sorted_text(
        url for item in ordered if (url := normalize_https_url(item.record.landing_url)) is not None
    )
    external_identifiers = _sorted_text(
        f"{item.observation.source_id}:{item.record.external_record_id}" for item in ordered
    )
    identifiers = tuple(
        CandidateIdentifier(kind=kind, value=value)
        for kind, values in (
            (IdentifierKind.DOI, dois),
            (IdentifierKind.URL, landing_urls),
            (IdentifierKind.EXTERNAL, external_identifiers),
        )
        for value in values
    )
    source_ids = tuple(sorted({item.observation.source_id for item in ordered}))
    categories = tuple(
        sorted({item.observation.category for item in ordered}, key=lambda item: item.value)
    )
    record_types = tuple(
        sorted({item.record.record_type for item in ordered}, key=lambda item: item.value)
    )
    published_dates = tuple(
        sorted({item.record.published_date for item in ordered if item.record.published_date})
    )
    license_labels = _sorted_text(
        normalize_license_label(item.record.license_label)
        for item in ordered
        if item.record.license_label is not None
    )
    file_formats = _sorted_text(
        normalized
        for item in ordered
        for value in item.record.file_formats
        if (normalized := normalize_file_format(value))
    )
    access_statuses = tuple(
        sorted({item.record.access_status for item in ordered}, key=lambda item: item.value)
    )
    observations = _unique_observations(ordered)
    primary_source = any(item.query.primary_source for item in ordered)
    preferred_title = _preferred_title(ordered)
    coverage_claims = _coverage_claims(ordered)
    conflicts = _metadata_conflicts(dedup_key, ordered)
    assessment = assess_source(
        primary_source=primary_source,
        access_statuses=access_statuses,
        license_labels=license_labels,
        file_formats=file_formats,
        has_title=bool(preferred_title),
        has_persistent_identifier=bool(dois),
        has_landing_url=bool(landing_urls),
        has_published_date=bool(published_dates),
    )
    candidate_id = f"src_{canonical_hash({'dedup_key': dedup_key})[:32]}"
    replica_group_key = f"replica:{canonical_hash({'key': dedup_key})[:32]}"

    def candidate_with_hash(candidate_hash: str) -> SourceCandidate:
        return SourceCandidate(
            candidate_id=candidate_id,
            dedup_key=dedup_key,
            replica_group_key=replica_group_key,
            preferred_title=preferred_title,
            identifiers=identifiers,
            landing_urls=landing_urls,
            source_ids=source_ids,
            categories=categories,
            primary_source=primary_source,
            record_types=record_types,
            published_dates=published_dates,
            license_labels=license_labels,
            file_formats=file_formats,
            access_statuses=access_statuses,
            observations=observations,
            coverage_claims=coverage_claims,
            conflicts=conflicts,
            assessment=assessment,
            candidate_hash=candidate_hash,
        )

    draft = candidate_with_hash("0" * 64)
    candidate_hash = calculate_source_candidate_hash(draft)
    return candidate_with_hash(candidate_hash)


def _sorted_text(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(values), key=lambda item: (item.casefold(), item)))


def _unique_observations(items: Sequence[ObservedRecord]) -> tuple[CandidateObservation, ...]:
    by_key: dict[tuple[object, ...], CandidateObservation] = {}
    for item in items:
        observation = item.observation
        key = (
            observation.query_id,
            observation.source_id,
            observation.external_record_id,
            observation.rank,
        )
        previous = by_key.get(key)
        if previous is not None and previous != observation:
            raise ValueError("duplicate observation key carries conflicting provenance")
        by_key[key] = observation
    return tuple(
        sorted(
            by_key.values(),
            key=lambda item: (
                item.query_id,
                item.source_id,
                item.connector_id,
                item.rank,
                item.external_record_id,
                item.raw_response_hash,
                item.evidence_ids,
            ),
        )
    )


def _preferred_title(items: Sequence[ObservedRecord]) -> str:
    preferred = min(
        items,
        key=lambda item: (
            not item.query.primary_source,
            item.observation.rank,
            -_record_metadata_count(item.record),
            normalize_title_key(item.record.title),
            normalize_title(item.record.title),
            item.observation.source_id,
            item.record.external_record_id,
        ),
    )
    return normalize_title(preferred.record.title)


def _record_metadata_count(record: ConnectorRecord) -> int:
    return sum(
        (
            normalize_doi(record.doi) is not None,
            normalize_https_url(record.landing_url) is not None,
            record.published_date is not None,
            record.license_label is not None,
            bool(record.file_formats),
            record.access_status.value != "unknown",
        )
    )


def _coverage_claims(items: Sequence[ObservedRecord]) -> tuple[CandidateCoverageClaim, ...]:
    evidence_by_field: dict[str, set[str]] = defaultdict(set)
    for item in items:
        for field_name in item.query.target_fields:
            evidence_by_field[field_name].update(item.observation.evidence_ids)
    return tuple(
        CandidateCoverageClaim(
            field_name=field_name,
            assessment=CoverageAssessment.PROBABLE,
            confidence=0.35,
            basis=CoverageBasis.QUERY_INTENT,
            evidence_ids=tuple(sorted(evidence_by_field[field_name])),
            explanation=(
                f"The discovery query targeted '{field_name}', so the source may contain this "
                "field; no scientific value was extracted or verified at this stage."
            ),
        )
        for field_name in sorted(evidence_by_field)
    )


def _metadata_conflicts(
    dedup_key: str, items: Sequence[ObservedRecord]
) -> tuple[MetadataConflict, ...]:
    mappings: tuple[tuple[str, dict[str, set[str]]], ...] = (
        ("title", _title_values(items)),
        (
            "doi",
            _metadata_values(items, lambda item: normalize_doi(item.record.doi)),
        ),
        (
            "published_date",
            _metadata_values(
                items,
                lambda item: (
                    item.record.published_date.isoformat()
                    if item.record.published_date is not None
                    else None
                ),
            ),
        ),
        (
            "license_label",
            _metadata_values(
                items,
                lambda item: (
                    normalize_license_label(item.record.license_label)
                    if item.record.license_label is not None
                    else None
                ),
            ),
        ),
        (
            "access_status",
            _metadata_values(items, lambda item: item.record.access_status.value),
        ),
        (
            "record_type",
            _metadata_values(items, lambda item: item.record.record_type.value),
        ),
    )
    conflicts: list[MetadataConflict] = []
    for field_name, values_to_evidence in mappings:
        if len(values_to_evidence) < 2:
            continue
        values = _sorted_text(values_to_evidence)
        evidence_ids = tuple(
            sorted({evidence_id for value in values for evidence_id in values_to_evidence[value]})
        )
        if len(evidence_ids) < 2:
            continue
        conflict_seed = {
            "dedup_key": dedup_key,
            "evidence_ids": evidence_ids,
            "field_name": field_name,
            "values": values,
        }
        conflicts.append(
            MetadataConflict(
                conflict_id=f"mcf_{canonical_hash(conflict_seed)[:16]}",
                field_name=field_name,
                values=values,
                evidence_ids=evidence_ids,
            )
        )
    return tuple(sorted(conflicts, key=lambda item: (item.field_name, item.conflict_id)))


def _title_values(items: Sequence[ObservedRecord]) -> dict[str, set[str]]:
    displays_by_key: dict[str, set[str]] = defaultdict(set)
    evidence_by_key: dict[str, set[str]] = defaultdict(set)
    for item in items:
        key = normalize_title_key(item.record.title)
        displays_by_key[key].add(normalize_title(item.record.title))
        evidence_by_key[key].update(item.observation.evidence_ids)
    if len(displays_by_key) < 2:
        return {}
    values: dict[str, set[str]] = {}
    for key in sorted(displays_by_key):
        display = min(displays_by_key[key], key=lambda item: (item.casefold(), item))
        values[display] = evidence_by_key[key]
    return values


def _metadata_values(
    items: Sequence[ObservedRecord],
    accessor: Callable[[ObservedRecord], str | None],
) -> dict[str, set[str]]:
    values: dict[str, set[str]] = defaultdict(set)
    for item in items:
        value = accessor(item)
        if value is not None:
            values[value].update(item.observation.evidence_ids)
    return values
