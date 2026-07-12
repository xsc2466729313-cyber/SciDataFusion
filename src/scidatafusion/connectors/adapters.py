"""Fixed-endpoint request builders and parsers for the M05 Connector registry."""

from __future__ import annotations

import hmac
import html
import json
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal, Protocol
from urllib.parse import urlsplit, urlunsplit

from scidatafusion.connectors.base import ResponseParseError
from scidatafusion.contracts.connectors import (
    AccessStatus,
    ConnectorDescriptor,
    ConnectorErrorCode,
    ConnectorParserKind,
    ConnectorRecord,
    SourceRecordType,
)
from scidatafusion.contracts.search import ExecutableQuery
from scidatafusion.domain.registry import canonical_hash

_MAX_TEXT = 8000
_MAX_CURSOR_LENGTH = 4096
_SPACE_PATTERN = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class ConnectorRequest:
    """Credential-free HTTP request description for one fixed endpoint."""

    method: Literal["GET", "POST"]
    url: str
    params: tuple[tuple[str, str], ...] = ()
    form: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ParsedConnectorPage:
    """Normalized records and an opaque token for another fixed-endpoint request."""

    records: tuple[ConnectorRecord, ...]
    next_page_token: str | None


class ConnectorAdapter(Protocol):
    """Source-specific code kept outside the source-neutral HTTP executor."""

    @property
    def descriptor(self) -> ConnectorDescriptor:
        """Return the descriptor validated against this adapter's fixed endpoint."""

    @property
    def parser_version(self) -> str:
        """Return the semantic parser version."""

    def build_request(
        self,
        query: ExecutableQuery,
        *,
        page_token: str | None,
        page_size: int,
    ) -> ConnectorRequest:
        """Build a request whose URL, path, and method cannot be vendor-controlled."""

    def parse_page(
        self,
        query: ExecutableQuery,
        content: bytes,
        *,
        page_token: str | None,
        page_size: int,
    ) -> ParsedConnectorPage:
        """Parse bounded untrusted bytes into strict Connector records."""


class _FixedEndpointAdapter:
    expected_endpoint: str
    expected_method: Literal["GET", "POST"]
    expected_parser: ConnectorParserKind
    parser_version = "1.0.0"

    def __init__(self, descriptor: ConnectorDescriptor) -> None:
        if descriptor.endpoint != self.expected_endpoint:
            raise ValueError("Connector descriptor does not match the adapter's fixed endpoint")
        if descriptor.readonly_method != self.expected_method:
            raise ValueError("Connector descriptor method does not match its adapter")
        if descriptor.parser is not self.expected_parser:
            raise ValueError("Connector descriptor parser does not match its adapter")
        self._descriptor = descriptor

    @property
    def descriptor(self) -> ConnectorDescriptor:
        return self._descriptor


class OpenAlexAdapter(_FixedEndpointAdapter):
    """OpenAlex Works cursor search adapter."""

    expected_endpoint = "https://api.openalex.org/works"
    expected_method = "GET"
    expected_parser = ConnectorParserKind.OPENALEX

    def build_request(
        self,
        query: ExecutableQuery,
        *,
        page_token: str | None,
        page_size: int,
    ) -> ConnectorRequest:
        return ConnectorRequest(
            method="GET",
            url=self.expected_endpoint,
            params=(
                ("search", query.query_text),
                ("per-page", str(min(page_size, 100))),
                ("cursor", page_token or "*"),
            ),
        )

    def parse_page(
        self,
        query: ExecutableQuery,
        content: bytes,
        *,
        page_token: str | None,
        page_size: int,
    ) -> ParsedConnectorPage:
        del query, page_token, page_size
        root = _decode_object(content)
        items = _object_list(root.get("results"), label="OpenAlex results")
        records: list[ConnectorRecord] = []
        for item in items:
            title = _text(item.get("display_name")) or _text(item.get("title"))
            external_id = _text(item.get("id"), limit=1024)
            if title is None or external_id is None:
                continue
            primary_location = _object_or_empty(item.get("primary_location"))
            best_location = _object_or_empty(item.get("best_oa_location"))
            open_access = _object_or_empty(item.get("open_access"))
            landing_url = _first_url(
                primary_location.get("landing_page_url"),
                best_location.get("landing_page_url"),
                item.get("id"),
            )
            doi = _doi(item.get("doi"))
            if doi is None:
                doi = _doi(_object_or_empty(item.get("ids")).get("doi"))
            pdf_url = _first_url(primary_location.get("pdf_url"), best_location.get("pdf_url"))
            license_label = _text(best_location.get("license"), limit=512)
            records.append(
                _record(
                    external_record_id=external_id,
                    record_type=SourceRecordType.PAPER,
                    title=title,
                    excerpt=_openalex_abstract(item.get("abstract_inverted_index")),
                    doi=doi,
                    landing_url=landing_url,
                    published_date=_iso_date(item.get("publication_date")),
                    license_label=license_label,
                    license_url=None,
                    file_formats=("pdf",) if pdf_url is not None else (),
                    access_status=(
                        AccessStatus.OPEN
                        if open_access.get("is_oa") is True
                        else AccessStatus.UNKNOWN
                    ),
                )
            )
        _require_parseable_items(items, records, "OpenAlex")
        meta = _object_or_empty(root.get("meta"))
        token = _page_token(meta.get("next_cursor")) if records else None
        return ParsedConnectorPage(records=_unique_records(records), next_page_token=token)


class ZenodoAdapter(_FixedEndpointAdapter):
    """Zenodo record search adapter with integer page tokens."""

    expected_endpoint = "https://zenodo.org/api/records"
    expected_method = "GET"
    expected_parser = ConnectorParserKind.ZENODO

    def build_request(
        self,
        query: ExecutableQuery,
        *,
        page_token: str | None,
        page_size: int,
    ) -> ConnectorRequest:
        page = _positive_page(page_token)
        return ConnectorRequest(
            method="GET",
            url=self.expected_endpoint,
            params=(
                ("q", query.query_text),
                ("size", str(min(page_size, 100))),
                ("page", str(page)),
            ),
        )

    def parse_page(
        self,
        query: ExecutableQuery,
        content: bytes,
        *,
        page_token: str | None,
        page_size: int,
    ) -> ParsedConnectorPage:
        del query
        root = _decode_object(content)
        hits = _require_object(root.get("hits"), label="Zenodo hits")
        items = _object_list(hits.get("hits"), label="Zenodo hit list")
        records: list[ConnectorRecord] = []
        for item in items:
            metadata = _object_or_empty(item.get("metadata"))
            title = _text(metadata.get("title"))
            external_id = _text(item.get("id"), limit=1024)
            if title is None or external_id is None:
                continue
            links = _object_or_empty(item.get("links"))
            license_value = metadata.get("license")
            license_object = _object_or_empty(license_value)
            license_label = (
                _text(license_object.get("id"), limit=512)
                or _text(license_object.get("title"), limit=512)
                or _text(license_value, limit=512)
            )
            access_right = (_text(metadata.get("access_right"), limit=64) or "").casefold()
            records.append(
                _record(
                    external_record_id=external_id,
                    record_type=_zenodo_record_type(metadata),
                    title=title,
                    excerpt=_text(metadata.get("description")),
                    doi=_doi(item.get("doi")) or _doi(metadata.get("doi")),
                    landing_url=_first_url(
                        links.get("html"),
                        links.get("self_html"),
                        links.get("latest_html"),
                    ),
                    published_date=_iso_date(metadata.get("publication_date")),
                    license_label=license_label,
                    license_url=_first_url(license_object.get("url")),
                    file_formats=_zenodo_formats(item.get("files")),
                    access_status=(
                        AccessStatus.OPEN
                        if access_right == "open"
                        else AccessStatus.RESTRICTED
                        if access_right in {"closed", "embargoed", "restricted"}
                        else AccessStatus.UNKNOWN
                    ),
                )
            )
        _require_parseable_items(items, records, "Zenodo")
        current_page = _positive_page(page_token)
        total = _total_hits(hits.get("total"))
        effective_page_size = min(page_size, 100)
        has_more = (
            bool(records) and total is not None and current_page * effective_page_size < total
        )
        token = str(current_page + 1) if has_more else None
        return ParsedConnectorPage(records=_unique_records(records), next_page_token=token)


class VizierTapAdapter(_FixedEndpointAdapter):
    """VizieR TAP_SCHEMA catalog discovery without executing vendor-provided ADQL."""

    expected_endpoint = "https://tapvizier.cds.unistra.fr/TAPVizieR/tap/sync"
    expected_method = "POST"
    expected_parser = ConnectorParserKind.VIZIER_TAP

    def build_request(
        self,
        query: ExecutableQuery,
        *,
        page_token: str | None,
        page_size: int,
    ) -> ConnectorRequest:
        if page_token is not None:
            raise ResponseParseError("VizieR discovery does not accept pagination tokens")
        terms = _query_terms(query)
        predicates = [
            "(LOWER(table_name) LIKE LOWER('%"
            + _adql_literal(term)
            + "%') OR LOWER(description) LIKE LOWER('%"
            + _adql_literal(term)
            + "%'))"
            for term in terms[:8]
        ]
        where = " OR ".join(predicates)
        # Only fixed TAP_SCHEMA identifiers and escaped ADQL literals enter this statement.
        adql = (
            f"SELECT TOP {min(page_size, 1000)} table_name, description "  # noqa: S608  # nosec B608
            f"FROM TAP_SCHEMA.tables WHERE {where}"
        )
        return ConnectorRequest(
            method="POST",
            url=self.expected_endpoint,
            form=(
                ("REQUEST", "doQuery"),
                ("LANG", "ADQL"),
                ("FORMAT", "json"),
                ("QUERY", adql),
            ),
        )

    def parse_page(
        self,
        query: ExecutableQuery,
        content: bytes,
        *,
        page_token: str | None,
        page_size: int,
    ) -> ParsedConnectorPage:
        del query, page_token, page_size
        root = _decode_object(content)
        rows = root.get("data")
        if not isinstance(rows, list):
            raise ResponseParseError(
                "VizieR response is missing a data array",
                code=ConnectorErrorCode.SCHEMA_DRIFT,
            )
        names = _vizier_column_names(root.get("metadata"))
        records: list[ConnectorRecord] = []
        for row in rows:
            values = _vizier_row(row, names)
            external_id = _text(values.get("table_name"), limit=1024)
            if external_id is None:
                continue
            records.append(
                _record(
                    external_record_id=external_id,
                    record_type=SourceRecordType.CATALOG,
                    title=external_id,
                    excerpt=_text(values.get("description")),
                    doi=None,
                    landing_url=None,
                    published_date=None,
                    license_label=None,
                    license_url=None,
                    file_formats=("votable", "fits", "csv"),
                    access_status=AccessStatus.OPEN,
                )
            )
        _require_parseable_items(rows, records, "VizieR")
        return ParsedConnectorPage(records=_unique_records(records), next_page_token=None)


class CrossrefAdapter(_FixedEndpointAdapter):
    """Crossref Works first-page adapter used to discover publication supplements."""

    expected_endpoint = "https://api.crossref.org/works"
    expected_method = "GET"
    expected_parser = ConnectorParserKind.CROSSREF

    def build_request(
        self,
        query: ExecutableQuery,
        *,
        page_token: str | None,
        page_size: int,
    ) -> ConnectorRequest:
        if page_token is not None:
            raise ResponseParseError("Crossref discovery does not accept pagination tokens")
        return ConnectorRequest(
            method="GET",
            url=self.expected_endpoint,
            params=(
                ("query.bibliographic", query.query_text),
                ("rows", str(min(page_size, 100))),
                ("cursor", page_token or "*"),
            ),
        )

    def parse_page(
        self,
        query: ExecutableQuery,
        content: bytes,
        *,
        page_token: str | None,
        page_size: int,
    ) -> ParsedConnectorPage:
        del query, page_token
        root = _decode_object(content)
        message = _require_object(root.get("message"), label="Crossref message")
        items = _object_list(message.get("items"), label="Crossref items")
        records: list[ConnectorRecord] = []
        for item in items:
            title = _first_text(item.get("title"))
            external_id = _text(item.get("DOI"), limit=1024) or _text(item.get("URL"), limit=1024)
            if title is None or external_id is None:
                continue
            licenses = _object_list_optional(item.get("license"))
            license_entry = licenses[0] if licenses else {}
            license_url = _first_url(license_entry.get("URL"))
            formats = _crossref_formats(item.get("link"))
            records.append(
                _record(
                    external_record_id=external_id,
                    record_type=SourceRecordType.PAPER,
                    title=title,
                    excerpt=_text(item.get("abstract")),
                    doi=_doi(item.get("DOI")),
                    landing_url=_first_url(item.get("URL")),
                    published_date=_crossref_date(item),
                    license_label=license_url,
                    license_url=license_url,
                    file_formats=formats,
                    access_status=(
                        AccessStatus.OPEN
                        if _is_recognized_open_license(license_url)
                        else AccessStatus.UNKNOWN
                    ),
                )
            )
        _require_parseable_items(items, records, "Crossref")
        del page_size
        return ParsedConnectorPage(records=_unique_records(records), next_page_token=None)


def adapter_for_descriptor(descriptor: ConnectorDescriptor) -> ConnectorAdapter:
    """Create the parser adapter selected by registry metadata, not orchestration logic."""

    adapters: dict[ConnectorParserKind, Callable[[ConnectorDescriptor], ConnectorAdapter]] = {
        ConnectorParserKind.OPENALEX: OpenAlexAdapter,
        ConnectorParserKind.ZENODO: ZenodoAdapter,
        ConnectorParserKind.VIZIER_TAP: VizierTapAdapter,
        ConnectorParserKind.CROSSREF: CrossrefAdapter,
    }
    adapter_type = adapters.get(descriptor.parser)
    if adapter_type is None:
        raise ValueError(f"No HTTP adapter is registered for parser {descriptor.parser.value}")
    return adapter_type(descriptor)


def calculate_connector_record_hash(record: ConnectorRecord) -> str:
    """Recalculate a record hash from its complete model payload except the hash field."""

    return canonical_hash(record.model_dump(mode="json", exclude={"record_hash"}))


def verify_connector_record_hash(record: ConnectorRecord) -> bool:
    """Return whether a Connector record is bound to its normalized model payload."""

    return hmac.compare_digest(record.record_hash, calculate_connector_record_hash(record))


def _decode_object(content: bytes) -> dict[str, object]:
    try:
        value: object = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResponseParseError("Connector response is not valid JSON") from exc
    return _require_object(value, label="response root")


def _require_object(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ResponseParseError(
            f"{label} must be a JSON object",
            code=ConnectorErrorCode.SCHEMA_DRIFT,
        )
    return {str(key): item for key, item in value.items()}


def _object_or_empty(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        return {}
    return {str(key): item for key, item in value.items()}


def _object_list(value: object, *, label: str) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ResponseParseError(
            f"{label} must be a JSON array",
            code=ConnectorErrorCode.SCHEMA_DRIFT,
        )
    return [_object_or_empty(item) for item in value if isinstance(item, dict)]


def _object_list_optional(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [_object_or_empty(item) for item in value if isinstance(item, dict)]


def _text(value: object, *, limit: int = _MAX_TEXT) -> str | None:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    normalized = unicodedata.normalize("NFKC", html.unescape(str(value)))
    normalized = _SPACE_PATTERN.sub(" ", normalized).strip()
    if not normalized:
        return None
    return normalized[:limit]


def _first_text(value: object) -> str | None:
    if isinstance(value, list):
        for item in value:
            text = _text(item)
            if text is not None:
                return text
        return None
    return _text(value)


def _https_url(value: object) -> str | None:
    text = _text(value, limit=4096)
    if text is None:
        return None
    parsed = urlsplit(text)
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        return None
    return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))


def _first_url(*values: object) -> str | None:
    return next((url for value in values if (url := _https_url(value)) is not None), None)


def _doi(value: object) -> str | None:
    text = _text(value, limit=512)
    if text is None:
        return None
    lowered = text.casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if lowered.startswith(prefix):
            text = text[len(prefix) :]
            break
    normalized = text.strip().casefold()
    return normalized or None


def _iso_date(value: object) -> date | None:
    text = _text(value, limit=32)
    if text is None or len(text) != 10:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _record(
    *,
    external_record_id: str,
    record_type: SourceRecordType,
    title: str,
    excerpt: str | None,
    doi: str | None,
    landing_url: str | None,
    published_date: date | None,
    license_label: str | None,
    license_url: str | None,
    file_formats: tuple[str, ...],
    access_status: AccessStatus,
) -> ConnectorRecord:
    unbound = ConnectorRecord(
        external_record_id=external_record_id,
        record_type=record_type,
        title=title,
        untrusted_excerpt=excerpt,
        doi=doi,
        landing_url=landing_url,
        published_date=published_date,
        license_label=license_label,
        license_url=license_url,
        file_formats=file_formats,
        access_status=access_status,
        record_hash="0" * 64,
    )
    return ConnectorRecord.model_validate(
        {
            **unbound.model_dump(mode="python"),
            "record_hash": calculate_connector_record_hash(unbound),
        }
    )


def _unique_records(records: list[ConnectorRecord]) -> tuple[ConnectorRecord, ...]:
    seen: set[str] = set()
    unique: list[ConnectorRecord] = []
    for record in records:
        if record.record_hash not in seen:
            seen.add(record.record_hash)
            unique.append(record)
    return tuple(unique)


def _require_parseable_items(
    items: Sequence[object], records: list[ConnectorRecord], source: str
) -> None:
    if items and not records:
        raise ResponseParseError(
            f"{source} returned records but none matched the expected schema",
            code=ConnectorErrorCode.SCHEMA_DRIFT,
        )


def _page_token(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    token = value.strip()
    if not token:
        return None
    if len(token) > _MAX_CURSOR_LENGTH:
        raise ResponseParseError("Connector pagination token exceeds the safety limit")
    return token


def _positive_page(value: str | None) -> int:
    if value is None:
        return 1
    try:
        page = int(value)
    except ValueError as exc:
        raise ResponseParseError("Zenodo pagination token must be an integer") from exc
    if page < 1 or page > 1_000_000:
        raise ResponseParseError("Zenodo pagination token is outside the allowed range")
    return page


def _total_hits(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    nested = _object_or_empty(value).get("value")
    if isinstance(nested, int) and not isinstance(nested, bool) and nested >= 0:
        return nested
    return None


def _openalex_abstract(value: object) -> str | None:
    inverted = _object_or_empty(value)
    positioned: list[tuple[int, str]] = []
    for word, positions in inverted.items():
        if not isinstance(word, str) or not isinstance(positions, list):
            continue
        for position in positions:
            if isinstance(position, int) and not isinstance(position, bool) and position >= 0:
                positioned.append((position, word))
    positioned.sort(key=lambda item: (item[0], item[1]))
    return _text(" ".join(word for _, word in positioned))


def _zenodo_formats(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    formats: list[str] = []
    for entry in value:
        item = _object_or_empty(entry)
        key = _text(item.get("key"), limit=1024)
        file_type = _text(item.get("type"), limit=128)
        candidate = None
        if key is not None and "." in key.rsplit("/", maxsplit=1)[-1]:
            candidate = key.rsplit(".", maxsplit=1)[-1]
        elif file_type is not None:
            candidate = file_type.rsplit("/", maxsplit=1)[-1]
        normalized = _text(candidate, limit=64)
        if normalized is not None:
            normalized = normalized.casefold()
            if normalized not in formats:
                formats.append(normalized)
    return tuple(formats)


def _zenodo_record_type(metadata: dict[str, object]) -> SourceRecordType:
    resource_type = _object_or_empty(metadata.get("resource_type"))
    value = (
        _text(resource_type.get("type"), limit=64)
        or _text(metadata.get("upload_type"), limit=64)
        or ""
    ).casefold()
    if value == "dataset":
        return SourceRecordType.DATASET
    if value == "publication":
        return SourceRecordType.PAPER
    return SourceRecordType.WEB


def _crossref_formats(value: object) -> tuple[str, ...]:
    formats: list[str] = []
    for item in _object_list_optional(value):
        content_type = _text(item.get("content-type"), limit=128)
        if content_type is None:
            continue
        normalized = content_type.rsplit("/", maxsplit=1)[-1].casefold()
        if normalized not in formats:
            formats.append(normalized)
    return tuple(formats)


def _is_recognized_open_license(value: str | None) -> bool:
    if value is None:
        return False
    parsed = urlsplit(value)
    host = (parsed.hostname or "").casefold().rstrip(".")
    path = parsed.path.casefold()
    if host in {"creativecommons.org", "www.creativecommons.org"}:
        return path.startswith(("/licenses/", "/publicdomain/"))
    if host in {"opensource.org", "www.opensource.org"}:
        return path.startswith("/licenses/")
    return False


def _crossref_date(item: dict[str, object]) -> date | None:
    for key in ("published-print", "published-online", "published", "issued"):
        container = _object_or_empty(item.get(key))
        parts = container.get("date-parts")
        if not isinstance(parts, list) or not parts or not isinstance(parts[0], list):
            continue
        first = parts[0]
        if len(first) < 3 or any(
            not isinstance(value, int) or isinstance(value, bool) for value in first[:3]
        ):
            continue
        try:
            return date(first[0], first[1], first[2])
        except ValueError:
            continue
    return None


def _query_terms(query: ExecutableQuery) -> tuple[str, ...]:
    terms = next((item.values for item in query.parameters if item.name == "terms"), ())
    return terms or (query.normalized_query,)


def _adql_literal(value: str) -> str:
    bounded = value.replace("\x00", " ")[:512]
    return bounded.replace("'", "''")


def _vizier_column_names(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ("table_name", "description")
    names = tuple(
        name
        for item in value
        if (name := _text(_object_or_empty(item).get("name"), limit=128)) is not None
    )
    return names or ("table_name", "description")


def _vizier_row(value: object, names: tuple[str, ...]) -> dict[str, object]:
    if isinstance(value, dict):
        return _object_or_empty(value)
    if isinstance(value, list):
        return {name: item for name, item in zip(names, value, strict=False)}
    return {}
