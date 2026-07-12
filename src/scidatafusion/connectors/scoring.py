"""Deterministic, metadata-only assessment for discovered source candidates."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection
from urllib.parse import unquote, urlsplit

from scidatafusion.contracts.connectors import (
    AccessStatus,
    ScoreComponent,
    SourceAssessment,
)

ASSESSMENT_POLICY_VERSION = "1.0.0"

_SPACE = re.compile(r"\s+")
_FORMAT_ALIASES = {
    "application/fits": "fits",
    "application/json": "json",
    "application/ld+json": "jsonld",
    "application/netcdf": "netcdf",
    "application/octet-stream+fits": "fits",
    "application/parquet": "parquet",
    "application/vnd.apache.arrow.file": "arrow",
    "application/x-fits": "fits",
    "application/x-hdf": "hdf5",
    "application/x-hdf5": "hdf5",
    "application/x-netcdf": "netcdf",
    "application/x-parquet": "parquet",
    "application/x-votable+xml": "votable",
    "application/xml": "xml",
    "image/fits": "fits",
    "text/csv": "csv",
    "text/tab-separated-values": "tsv",
    "text/xml": "xml",
    "vnd.apache.parquet": "parquet",
}
_FORMAT_NAMES = {
    "fit": "fits",
    "fits": "fits",
    "fts": "fits",
    "h5": "hdf5",
    "hdf": "hdf5",
    "hdf5": "hdf5",
    "jpeg": "jpg",
    "json-lines": "jsonl",
    "json_lines": "jsonl",
    "ndjson": "jsonl",
    "netcdf4": "netcdf",
    "nc": "netcdf",
    "parq": "parquet",
    "tiff": "tif",
    "vot": "votable",
}
_MACHINE_READABLE_FORMATS = {
    "arrow",
    "csv",
    "ecsv",
    "feather",
    "fits",
    "hdf5",
    "json",
    "jsonl",
    "jsonld",
    "netcdf",
    "parquet",
    "tsv",
    "votable",
    "xml",
}
_INSPECTABLE_FORMATS = {
    "doc",
    "docx",
    "html",
    "jpg",
    "pdf",
    "png",
    "tif",
    "txt",
    "xls",
    "xlsx",
    "zip",
}
_LICENSE_ALIASES = {
    "apache 2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache-2.0": "Apache-2.0",
    "bsd 2-clause": "BSD-2-Clause",
    "bsd 3-clause": "BSD-3-Clause",
    "cc by 3.0": "CC-BY-3.0",
    "cc by 4.0": "CC-BY-4.0",
    "cc by-sa 3.0": "CC-BY-SA-3.0",
    "cc by-sa 4.0": "CC-BY-SA-4.0",
    "cc0": "CC0-1.0",
    "cc0 1.0": "CC0-1.0",
    "cc0-1.0": "CC0-1.0",
    "creative commons attribution 3.0": "CC-BY-3.0",
    "creative commons attribution 4.0": "CC-BY-4.0",
    "mit": "MIT",
    "mit license": "MIT",
    "odc-by-1.0": "ODC-By-1.0",
    "odbl-1.0": "ODbL-1.0",
    "pddl-1.0": "PDDL-1.0",
}
_OPEN_LICENSES = frozenset(_LICENSE_ALIASES.values())
_RESTRICTED_LICENSE_MARKERS = (
    "all rights reserved",
    "closed",
    "proprietary",
    "restricted",
)


def normalize_file_format(value: str) -> str:
    """Return a stable format label without claiming unknown aliases are equivalent."""

    normalized = _SPACE.sub(" ", unicodedata.normalize("NFKC", value)).strip().casefold()
    normalized = normalized.removeprefix(".")
    if ";" in normalized:
        normalized = normalized.split(";", maxsplit=1)[0].strip()
    return _FORMAT_ALIASES.get(normalized, _FORMAT_NAMES.get(normalized, normalized))


def normalize_license_label(value: str) -> str:
    """Canonicalize known licenses while preserving an unrecognized label as metadata."""

    normalized = _SPACE.sub(" ", unicodedata.normalize("NFKC", value)).strip()
    folded = normalized.casefold().rstrip("/")
    alias = _LICENSE_ALIASES.get(folded)
    if alias is not None:
        return alias

    parsed = urlsplit(normalized)
    if parsed.hostname and parsed.hostname.casefold() == "creativecommons.org":
        path = unquote(parsed.path).strip("/").casefold()
        parts = path.split("/")
        if len(parts) >= 3 and parts[0] == "licenses":
            license_name = parts[1]
            version = parts[2]
            creative_commons = {
                "by": f"CC-BY-{version}",
                "by-sa": f"CC-BY-SA-{version}",
            }
            return creative_commons.get(license_name, normalized)
        if parts[:3] == ["publicdomain", "zero", "1.0"]:
            return "CC0-1.0"
    return normalized


def assess_source(
    *,
    primary_source: bool,
    access_statuses: Collection[AccessStatus],
    license_labels: Collection[str],
    file_formats: Collection[str],
    has_title: bool,
    has_persistent_identifier: bool,
    has_landing_url: bool,
    has_published_date: bool,
) -> SourceAssessment:
    """Score only observable discovery metadata using a fixed, transparent policy."""

    access_value, access_rationale = _score_access(access_statuses)
    license_value, license_rationale = _score_license(license_labels)
    format_value, format_rationale = _score_formats(file_formats)
    metadata_signals = (
        has_title,
        has_persistent_identifier,
        has_landing_url,
        has_published_date,
        bool(license_labels),
        bool(file_formats),
        any(status is not AccessStatus.UNKNOWN for status in access_statuses),
    )
    metadata_value = sum(metadata_signals) / len(metadata_signals)

    components = (
        ScoreComponent(
            name="primary_source",
            value=1.0 if primary_source else 0.0,
            weight=0.25,
            rationale=(
                "At least one planned query marked the source as primary."
                if primary_source
                else "No planned query marked the source as primary."
            ),
        ),
        ScoreComponent(
            name="access_status",
            value=access_value,
            weight=0.20,
            rationale=access_rationale,
        ),
        ScoreComponent(
            name="license_clarity",
            value=license_value,
            weight=0.15,
            rationale=license_rationale,
        ),
        ScoreComponent(
            name="usable_format",
            value=format_value,
            weight=0.20,
            rationale=format_rationale,
        ),
        ScoreComponent(
            name="metadata_completeness",
            value=metadata_value,
            weight=0.20,
            rationale=(
                f"{sum(metadata_signals)} of {len(metadata_signals)} discovery metadata "
                "signals are present; this does not assess scientific values."
            ),
        ),
    )
    total_score = sum(component.value * component.weight for component in components)
    return SourceAssessment(
        policy_version=ASSESSMENT_POLICY_VERSION,
        components=components,
        total_score=total_score,
    )


def _score_access(statuses: Collection[AccessStatus]) -> tuple[float, str]:
    if AccessStatus.OPEN in statuses:
        return 1.0, "At least one discovered replica is explicitly marked open."
    if AccessStatus.RESTRICTED in statuses:
        return 0.0, "Discovered access metadata is restricted and no open replica is known."
    return 0.0, "Access is unknown; no openness is inferred."


def _score_license(labels: Collection[str]) -> tuple[float, str]:
    if not labels:
        return 0.0, "No license metadata is present; reuse permission is not inferred."
    folded = tuple(label.casefold() for label in labels)
    if any(marker in label for label in folded for marker in _RESTRICTED_LICENSE_MARKERS):
        return 0.0, "License metadata includes a restricted or proprietary marker."
    recognized = sum(label in _OPEN_LICENSES for label in labels)
    if recognized == len(labels):
        return 1.0, "Every distinct license label is a recognized open license."
    if recognized:
        return 0.5, "An open license is present, but another label is unrecognized."
    return 0.0, "License labels are unrecognized; reuse permission is not inferred."


def _score_formats(formats: Collection[str]) -> tuple[float, str]:
    if any(item in _MACHINE_READABLE_FORMATS for item in formats):
        return 1.0, "At least one declared format is machine-readable for scientific processing."
    if any(item in _INSPECTABLE_FORMATS for item in formats):
        return 0.35, "Declared formats are inspectable but need extraction or conversion."
    if formats:
        return 0.0, "Declared formats are unrecognized; machine readability is not inferred."
    return 0.0, "No file format metadata is present."
