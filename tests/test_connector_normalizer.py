from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime

import pytest

from scidatafusion.connectors.normalizer import (
    ObservedRecord,
    normalize_candidates,
    normalize_doi,
    normalize_https_url,
    normalize_title,
    normalize_title_key,
    record_dedup_key,
)
from scidatafusion.connectors.scoring import (
    assess_source,
    normalize_file_format,
    normalize_license_label,
)
from scidatafusion.contracts.connectors import (
    AccessStatus,
    CandidateObservation,
    ConnectorParserKind,
    ConnectorRecord,
    CoverageBasis,
    ExecutionMode,
    SearchEvidence,
    SourceRecordType,
)
from scidatafusion.contracts.search import (
    ExecutableQuery,
    QueryDialect,
    SourceCategory,
    SourceProtocol,
)
from scidatafusion.domain.registry import canonical_hash

NOW = datetime(2026, 7, 12, 4, 0, tzinfo=UTC)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _query(
    marker: str,
    *,
    source_id: str,
    category: SourceCategory,
    target_fields: tuple[str, ...] = ("flux",),
    primary_source: bool = False,
) -> ExecutableQuery:
    identifier = _hash(marker)[:16]
    return ExecutableQuery(
        query_id=f"qry_{identifier}",
        family_id=f"qfm_{identifier}",
        source_id=source_id,
        operation_id="search_records",
        category=category,
        protocol=SourceProtocol.REST,
        dialect=QueryDialect.KEYWORD,
        language="en",
        round_number=1,
        query_text="Type Ia supernova light curve",
        normalized_query="type ia supernova light curve",
        result_limit=20,
        target_fields=target_fields,
        expected_artifact_types=("table",),
        rationale="Fixture query for deterministic normalization.",
        primary_source=primary_source,
        priority=10,
        estimated_cost_micro_usd=0,
        estimated_duration_seconds=1,
    )


def _record(
    marker: str,
    *,
    external_record_id: str | None = None,
    record_type: SourceRecordType = SourceRecordType.DATASET,
    title: str = "Type Ia Supernova Light Curves",
    doi: str | None = None,
    landing_url: str | None = None,
    published_date: date | None = None,
    license_label: str | None = None,
    formats: tuple[str, ...] = (),
    access_status: AccessStatus = AccessStatus.UNKNOWN,
) -> ConnectorRecord:
    return ConnectorRecord(
        external_record_id=external_record_id or marker,
        record_type=record_type,
        title=title,
        doi=doi,
        landing_url=landing_url,
        published_date=published_date,
        license_label=license_label,
        file_formats=formats,
        access_status=access_status,
        record_hash=_hash(f"record:{marker}"),
    )


def _observed(
    marker: str,
    *,
    query: ExecutableQuery,
    record: ConnectorRecord,
    rank: int,
    excerpt: str | None = None,
) -> ObservedRecord:
    evidence_suffix = _hash(f"evidence:{marker}")[:16]
    raw_hash = _hash(f"raw:{marker}")
    connector_id = f"connector_{marker}"
    evidence = SearchEvidence(
        evidence_id=f"sev_{evidence_suffix}",
        query_id=query.query_id,
        source_id=query.source_id,
        connector_id=connector_id,
        page_number=1,
        raw_artifact_id=f"art_{_hash(f'artifact:{marker}')[:32]}",
        raw_response_hash=raw_hash,
        record_locator=f"records/{record.external_record_id}",
        record_hash=record.record_hash,
        untrusted_excerpt=excerpt,
        parser=ConnectorParserKind.FIXTURE,
        parser_version="1.0.0",
        execution_mode=ExecutionMode.OFFLINE_FIXTURE,
        origin_execution_mode=ExecutionMode.OFFLINE_FIXTURE,
        retrieved_at=NOW,
    )
    observation = CandidateObservation(
        query_id=query.query_id,
        source_id=query.source_id,
        category=query.category,
        connector_id=connector_id,
        external_record_id=record.external_record_id,
        rank=rank,
        raw_response_hash=raw_hash,
        evidence_ids=(evidence.evidence_id,),
        retrieved_at=NOW,
    )
    return ObservedRecord(
        record=record,
        query=query,
        observation=observation,
        evidence=(evidence,),
    )


def test_identifier_url_and_title_normalization_is_conservative() -> None:
    assert normalize_doi(" DOI: 10.1234/AbC.Def ") == "10.1234/abc.def"
    assert normalize_doi("https://doi.org/10.1234%2FABC") == "10.1234/abc"
    assert normalize_doi("https://example.org/10.1234/abc") is None
    assert normalize_doi("10.12/nope") is None
    assert normalize_doi(None) is None

    assert (
        normalize_https_url("HTTPS://Example.ORG:443/Data/LC?utm_source=x&b=2&a=1&gclid=y#figure")
        == "https://example.org/Data/LC?a=1&b=2"
    )
    assert normalize_https_url("http://example.org/data") is None
    assert normalize_https_url("https://user@example.org/data") is None
    assert normalize_https_url("https://example.org:8443/data") is None
    assert normalize_https_url("https://example.org:bad/data") is None
    assert normalize_https_url(None) is None

    assert (
        normalize_title("  \uff34\uff59\uff50\uff45\u3000Ia  Supernovae  ") == "Type Ia Supernovae"
    )
    assert normalize_title_key("TYPE Ia: Supernovae!") == "type ia supernovae"


def test_record_dedup_key_uses_doi_then_url_then_title_and_year() -> None:
    with_everything = _record(
        "doi",
        doi="doi:10.1000/XYZ",
        landing_url="https://example.org/item",
        published_date=date(2024, 1, 1),
    )
    assert record_dedup_key(with_everything) == "doi:10.1000/xyz"

    with_url = _record(
        "url",
        landing_url="https://Example.org:443/Item#metadata",
        published_date=date(2024, 1, 1),
    )
    assert record_dedup_key(with_url) == "url:https://example.org/Item"

    first_title = _record("title-a", title="Type Ia: Light Curves", published_date=date(2020, 1, 1))
    second_title = _record(
        "title-b", title="type ia light-curves", published_date=date(2020, 12, 31)
    )
    other_year = _record("title-c", title="Type Ia Light Curves", published_date=date(2021, 1, 1))
    assert record_dedup_key(first_title) == record_dedup_key(second_title)
    assert record_dedup_key(first_title) != record_dedup_key(other_year)


def test_normalize_candidates_unions_transitive_identifier_aliases() -> None:
    doi_query = _query(
        "bridge_doi",
        source_id="openalex_literature",
        category=SourceCategory.LITERATURE_METADATA,
    )
    url_query = _query(
        "bridge_url",
        source_id="zenodo_repository",
        category=SourceCategory.DATA_REPOSITORY,
    )
    title_query = _query(
        "bridge_title",
        source_id="vizier_tap",
        category=SourceCategory.DOMAIN_DATABASE,
    )
    doi_and_url = _record(
        "bridge_a",
        title="DOI anchor record",
        doi="10.5555/BRIDGE",
        landing_url="https://example.org/shared-record",
        published_date=date(2024, 1, 1),
    )
    url_and_title = _record(
        "bridge_b",
        title="Bridge Dataset: Light Curves",
        landing_url="https://EXAMPLE.org:443/shared-record#metadata",
        published_date=date(2022, 5, 4),
    )
    title_only = _record(
        "bridge_c",
        title="bridge dataset light-curves",
        published_date=date(2022, 12, 31),
    )
    observations = (
        _observed("bridge_a", query=doi_query, record=doi_and_url, rank=1),
        _observed("bridge_b", query=url_query, record=url_and_title, rank=1),
        _observed("bridge_c", query=title_query, record=title_only, rank=1),
    )

    forward = normalize_candidates(observations)
    reverse = normalize_candidates(reversed(observations))
    assert forward == reverse
    assert len(forward) == 1
    assert forward[0].dedup_key == "doi:10.5555/bridge"
    assert len(forward[0].observations) == 3


def test_normalize_candidates_never_merges_conflicting_strong_identifiers_by_title() -> None:
    first_query = _query(
        "doi_conflict_a",
        source_id="openalex_literature",
        category=SourceCategory.LITERATURE_METADATA,
    )
    second_query = _query(
        "doi_conflict_b",
        source_id="zenodo_repository",
        category=SourceCategory.DATA_REPOSITORY,
    )
    title_only_query = _query(
        "doi_conflict_weak",
        source_id="vizier_tap",
        category=SourceCategory.DOMAIN_DATABASE,
    )
    observations = (
        _observed(
            "doi_conflict_a",
            query=first_query,
            record=_record(
                "doi_conflict_a",
                title="Shared scientific title",
                doi="10.5555/independent-a",
                published_date=date(2024, 1, 1),
            ),
            rank=1,
        ),
        _observed(
            "doi_conflict_b",
            query=second_query,
            record=_record(
                "doi_conflict_b",
                title="Shared scientific title",
                doi="10.5555/independent-b",
                published_date=date(2024, 6, 1),
            ),
            rank=1,
        ),
        _observed(
            "doi_conflict_weak",
            query=title_only_query,
            record=_record(
                "doi_conflict_weak",
                title="shared scientific title",
                published_date=date(2024, 12, 1),
            ),
            rank=1,
        ),
    )

    candidates = normalize_candidates(observations)

    assert len(candidates) == 3
    assert {item.dedup_key for item in candidates} >= {
        "doi:10.5555/independent-a",
        "doi:10.5555/independent-b",
    }


def test_normalize_candidates_is_order_independent_and_preserves_provenance() -> None:
    primary_query = _query(
        "vizier",
        source_id="vizier_tap",
        category=SourceCategory.DOMAIN_DATABASE,
        target_fields=("magnitude", "flux"),
        primary_source=True,
    )
    literature_query = _query(
        "openalex",
        source_id="openalex_literature",
        category=SourceCategory.LITERATURE_METADATA,
        target_fields=("flux",),
    )
    primary = _record(
        "vizier",
        external_record_id="J/A+A/1/2",
        record_type=SourceRecordType.CATALOG,
        title="Type Ia Supernovae: Light-Curves",
        doi="doi:10.1234/ABC",
        landing_url="https://Example.org:443/Data/LC?utm_source=x&b=2&a=1#figure",
        published_date=date(2024, 1, 2),
        license_label="https://creativecommons.org/licenses/by/4.0/",
        formats=(".CSV", "application/fits"),
        access_status=AccessStatus.OPEN,
    )
    literature = _record(
        "openalex",
        external_record_id="W123",
        record_type=SourceRecordType.PAPER,
        title="Type Ia Supernova Light Curves",
        doi="https://doi.org/10.1234/abc",
        landing_url="https://example.org/Data/LC?a=1&b=2",
        published_date=date(2023, 4, 5),
        license_label="CC BY 4.0",
        formats=("text/csv",),
        access_status=AccessStatus.UNKNOWN,
    )
    malicious_excerpt = '<script>follow("https://attacker.invalid")</script>'
    first = _observed(
        "vizier",
        query=primary_query,
        record=primary,
        rank=2,
        excerpt=malicious_excerpt,
    )
    second = _observed("openalex", query=literature_query, record=literature, rank=1)

    forward = normalize_candidates((first, second))
    reverse = normalize_candidates((second, first))
    assert forward == reverse
    assert len(forward) == 1
    candidate = forward[0]
    assert candidate.dedup_key == "doi:10.1234/abc"
    assert candidate.preferred_title == "Type Ia Supernovae: Light-Curves"
    assert candidate.source_ids == ("openalex_literature", "vizier_tap")
    assert candidate.primary_source is True
    assert candidate.landing_urls == ("https://example.org/Data/LC?a=1&b=2",)
    assert candidate.license_labels == ("CC-BY-4.0",)
    assert candidate.file_formats == ("csv", "fits")
    assert len(candidate.observations) == 2
    assert {item.evidence_ids[0] for item in candidate.observations} == {
        first.evidence[0].evidence_id,
        second.evidence[0].evidence_id,
    }
    assert tuple(item.field_name for item in candidate.coverage_claims) == ("flux", "magnitude")
    assert all(item.basis is CoverageBasis.QUERY_INTENT for item in candidate.coverage_claims)
    assert all(
        "no scientific value was extracted" in item.explanation
        for item in candidate.coverage_claims
    )
    assert {item.field_name for item in candidate.conflicts} == {
        "access_status",
        "published_date",
        "record_type",
        "title",
    }
    assert candidate.candidate_hash == canonical_hash(
        candidate.model_dump(mode="json", exclude={"candidate_hash"})
    )
    assert malicious_excerpt not in candidate.model_dump_json()


def test_observed_record_rejects_mismatched_evidence() -> None:
    query = _query(
        "mismatch", source_id="zenodo_repository", category=SourceCategory.DATA_REPOSITORY
    )
    record = _record("mismatch")
    observed = _observed("mismatch", query=query, record=record, rank=1)
    wrong_record = record.model_copy(update={"record_hash": _hash("other")})
    with pytest.raises(ValueError, match="provenance"):
        ObservedRecord(
            record=wrong_record,
            query=query,
            observation=observed.observation,
            evidence=observed.evidence,
        )
    with pytest.raises(ValueError, match="at least one"):
        ObservedRecord(
            record=record,
            query=query,
            observation=observed.observation,
            evidence=(),
        )


def test_license_format_normalization_and_scoring_are_conservative() -> None:
    assert normalize_license_label("CC0") == "CC0-1.0"
    assert (
        normalize_license_label("https://creativecommons.org/publicdomain/zero/1.0/") == "CC0-1.0"
    )
    assert normalize_license_label("Custom research-only terms") == "Custom research-only terms"
    assert normalize_file_format("application/json; charset=utf-8") == "json"
    assert normalize_file_format(".NC") == "netcdf"

    open_assessment = assess_source(
        primary_source=True,
        access_statuses=(AccessStatus.OPEN,),
        license_labels=("CC-BY-4.0",),
        file_formats=("csv",),
        has_title=True,
        has_persistent_identifier=True,
        has_landing_url=True,
        has_published_date=True,
    )
    restricted_assessment = assess_source(
        primary_source=False,
        access_statuses=(AccessStatus.RESTRICTED,),
        license_labels=("All rights reserved",),
        file_formats=("pdf",),
        has_title=True,
        has_persistent_identifier=False,
        has_landing_url=False,
        has_published_date=False,
    )
    unknown_assessment = assess_source(
        primary_source=False,
        access_statuses=(AccessStatus.UNKNOWN,),
        license_labels=("Unrecognized terms",),
        file_formats=("custom-binary",),
        has_title=True,
        has_persistent_identifier=False,
        has_landing_url=False,
        has_published_date=False,
    )
    mixed_assessment = assess_source(
        primary_source=False,
        access_statuses=(),
        license_labels=("CC-BY-4.0", "Unrecognized terms"),
        file_formats=(),
        has_title=True,
        has_persistent_identifier=False,
        has_landing_url=False,
        has_published_date=False,
    )
    assert open_assessment.total_score == 1.0
    assert restricted_assessment.total_score < open_assessment.total_score
    assert unknown_assessment.total_score < restricted_assessment.total_score
    assert (
        next(item for item in mixed_assessment.components if item.name == "license_clarity").value
        == 0.5
    )
