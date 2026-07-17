"""M27 bounded structured-data preview tests."""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

import scidatafusion.online.structured as structured_module
from scidatafusion.contracts.online import OnlineAcquiredArtifact
from scidatafusion.contracts.structured import OnlineStructuredDataResult
from scidatafusion.online.structured import OnlineStructuredDataService


def _artifact(
    payload: bytes,
    *,
    media_type: str = "text/csv",
    url: str = "https://data.example.org/records.csv",
    claimed_hash: str | None = None,
) -> OnlineAcquiredArtifact:
    byte_sha256 = claimed_hash or hashlib.sha256(payload).hexdigest()
    return OnlineAcquiredArtifact.model_validate(
        {
            "source_url": url,
            "source_title": "Current-topic records",
            "locator_hash": "1" * 64,
            "byte_sha256": byte_sha256,
            "size_bytes": len(payload),
            "media_type": media_type,
            "artifact_kind": "structured_table",
            "storage_uri": f"bronze://sha256/{byte_sha256}",
        }
    )


def _parse(payload: bytes, **artifact_kwargs: str) -> OnlineStructuredDataResult:
    artifact = _artifact(payload, **artifact_kwargs)
    return OnlineStructuredDataService().parse((artifact,), lambda _: payload)


def test_csv_preview_preserves_text_and_empty_values() -> None:
    payload = b'name,flux,note\nSN 1,1.20," x "\nSN 2,,ok\n'

    first = _parse(payload)
    second = _parse(payload)

    assert first == second
    assert not first.failures
    dataset = first.datasets[0]
    assert dataset.format == "csv"
    assert dataset.row_count == 2
    assert dataset.column_count == 3
    assert [item.name for item in dataset.columns] == ["name", "flux", "note"]
    assert dataset.columns[1].non_empty_count == 1
    assert dataset.columns[1].empty_count == 1
    assert dataset.columns[1].null_count == 0
    values = {(item.row_index, item.column_name): item.raw_value_json for item in dataset.cells}
    assert values[(1, "flux")] == '"1.20"'
    assert values[(1, "note")] == '" x "'
    assert values[(2, "flux")] == '""'
    assert dataset.cells[0].source_location == "第 2 行, 第 1 列"


def test_json_preview_retains_number_lexemes_and_nulls() -> None:
    payload = b'[{"time":1.20,"flag":true,"note":" x "},{"time":2,"flag":false}]'

    result = _parse(
        payload,
        media_type="application/json",
        url="https://data.example.org/records.json",
    )

    dataset = result.datasets[0]
    values = {(item.row_index, item.column_name): item.raw_value_json for item in dataset.cells}
    assert values[(1, "time")] == "1.20"
    assert values[(2, "time")] == "2"
    assert values[(1, "flag")] == "true"
    assert values[(1, "note")] == '" x "'
    assert values[(2, "note")] == "null"
    assert dataset.columns[2].null_count == 1
    assert dataset.cells[0].source_location == "$[0].time"


def test_tsv_can_be_detected_from_the_source_suffix() -> None:
    payload = b"object\tvalue\nA\t3\n"
    result = _parse(
        payload,
        media_type="text/plain",
        url="https://data.example.org/table.tsv",
    )

    assert result.datasets[0].format == "tsv"
    assert result.datasets[0].cells[-1].raw_value_json == '"3"'


def test_geojson_projects_only_scalar_feature_properties() -> None:
    payload = json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [1, 2]},
                    "properties": {"sample": "A", "measurement": 4},
                }
            ],
        },
        separators=(",", ":"),
    ).encode()
    result = _parse(
        payload,
        media_type="application/geo+json",
        url="https://data.example.org/points.geojson",
    )

    dataset = result.datasets[0]
    assert [item.name for item in dataset.columns] == ["sample", "measurement"]
    assert dataset.cells[0].source_location == "$.features[*].properties[0].sample"


@pytest.mark.parametrize(
    ("payload", "media_type", "url", "expected_code"),
    [
        (b"a,a\n1,2\n", "text/csv", "https://data.example.org/a.csv", "invalid_structure"),
        (b"a,b\n1\n", "text/csv", "https://data.example.org/a.csv", "invalid_structure"),
        (
            b'[{"a":{"nested":1}}]',
            "application/json",
            "https://data.example.org/a.json",
            "invalid_structure",
        ),
        (b"\xff\xfe", "text/csv", "https://data.example.org/a.csv", "invalid_encoding"),
        (b"PDF", "application/pdf", "https://data.example.org/a.pdf", "unsupported_media_type"),
    ],
)
def test_unsafe_or_unsupported_structures_fail_closed(
    payload: bytes, media_type: str, url: str, expected_code: str
) -> None:
    result = _parse(payload, media_type=media_type, url=url)

    assert not result.datasets
    assert result.failures[0].code == expected_code


def test_hash_mismatch_is_visible() -> None:
    payload = b"a\n1\n"
    artifact = _artifact(payload, claimed_hash="0" * 64)

    result = OnlineStructuredDataService().parse((artifact,), lambda _: payload)

    assert result.failures[0].code == "hash_mismatch"


def test_preview_is_bounded_without_changing_total_shape() -> None:
    columns = [f"c{index}" for index in range(22)]
    rows = [[f"r{row}c{column}" for column in range(22)] for row in range(25)]
    payload = (",".join(columns) + "\n" + "\n".join(",".join(row) for row in rows)).encode()

    dataset = _parse(payload).datasets[0]

    assert dataset.row_count == 25
    assert dataset.column_count == 22
    assert dataset.preview_row_count == 20
    assert dataset.preview_column_count == 20
    assert len(dataset.cells) == 400
    assert dataset.truncated


def test_total_cell_limit_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(structured_module, "_MAX_CELLS", 3)

    result = _parse(b"a,b\n1,2\n3,4\n")

    assert not result.datasets
    assert result.failures[0].code == "limit_exceeded"


def test_structured_parser_covers_bounded_failure_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _parse(b"a\t b\n1\t2\n", media_type="text/tab-separated-values").failures
    assert _parse(b"a\n1\n", media_type="text/plain", url="https://x.test/a.csv").datasets
    assert _parse(b'[{"a":1}]', media_type="text/plain", url="https://x.test/a.json").datasets
    with pytest.raises(structured_module._StructuredParseError, match="\u8868\u5934"):
        structured_module._parse_delimited("", "csv")
    assert _parse(b"[1]", media_type="application/json").failures[0].code == "invalid_structure"
    assert (
        _parse(b'{"unknown":1}', media_type="application/json").failures[0].code
        == "invalid_structure"
    )
    assert _parse(b'{"data":[{"a":1}]}', media_type="application/json").datasets
    assert _parse(b'{"results":[{"a":1}]}', media_type="application/json").datasets
    assert _parse(b"[]", media_type="application/json").failures[0].code == "invalid_structure"
    assert _parse(b'a\n"' + b"x" * 8_193 + b'"\n').failures[0].code == "limit_exceeded"
    assert (
        _parse(b'[{"a":NaN}]', media_type="application/json").failures[0].code
        == "invalid_structure"
    )
    invalid_geojson = b'{"type":"FeatureCollection","features":[{"properties":1}]}'
    assert _parse(invalid_geojson, media_type="application/geo+json").failures

    monkeypatch.setattr(structured_module, "_MAX_ROWS", 1)
    assert _parse(b"a\n1\n2\n").failures[0].code == "limit_exceeded"
    assert (
        _parse(b'[{"a":1},{"a":2}]', media_type="application/json").failures[0].code
        == "limit_exceeded"
    )
    monkeypatch.setattr(structured_module, "_MAX_ROWS", 100_000)
    monkeypatch.setattr(structured_module, "_MAX_COLUMNS", 1)
    assert _parse(b"a,b\n1,2\n").failures[0].code == "limit_exceeded"


def test_unexpected_parser_value_error_is_accounted_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"a\n1\n"
    service = OnlineStructuredDataService()
    monkeypatch.setattr(service, "_parse_artifact", lambda *_: (_ for _ in ()).throw(ValueError()))

    result = service.parse((_artifact(payload),), lambda _: payload)

    assert result.failures[0].code == "invalid_structure"


def test_structured_contracts_reject_extra_or_inconsistent_payloads() -> None:
    result = _parse(b"a\n1\n")
    payload = result.model_dump(mode="json")
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        OnlineStructuredDataResult.model_validate(payload)

    dataset_payload = result.datasets[0].model_dump(mode="json")
    dataset_payload["preview_row_count"] = 0
    with pytest.raises(ValidationError):
        type(result.datasets[0]).model_validate(dataset_payload)


def test_internal_json_column_guard_rejects_non_string_keys() -> None:
    with pytest.raises(structured_module._StructuredParseError, match="JSON"):
        structured_module._ordered_json_columns(({1: "value"},))
