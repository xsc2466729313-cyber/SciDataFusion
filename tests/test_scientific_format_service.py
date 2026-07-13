"""M12 FITS parsing, replay, and failure-boundary tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from io import BytesIO

import pytest
from pydantic import ValidationError

from scidatafusion.artifacts.storage import MemoryBronzeStore
from scidatafusion.cli import _build_search_planning
from scidatafusion.contracts.datasets import (
    ScalarKind,
    ScientificParsingRequest,
    ScientificParsingResult,
    ScientificParsingStatus,
    ScientificSubset,
    TransformationKind,
)
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.scientific_formats.checkpoints import MemoryScientificCheckpointStore
from scidatafusion.scientific_formats.fits import FitsParser
from scidatafusion.scientific_formats.fixtures import build_offline_scientific_bundle
from scidatafusion.scientific_formats.registry import PluginParserRegistry
from scidatafusion.scientific_formats.service import ScientificParsingService
from scidatafusion.scientific_formats.storage import MemoryDatasetIRStore


@dataclass(frozen=True)
class _Chain:
    store: MemoryBronzeStore
    dataset_store: MemoryDatasetIRStore
    request: ScientificParsingRequest


@pytest.fixture(scope="module")
def chain() -> _Chain:
    phase1, _ = _build_search_planning(
        "Study Type Ia supernova light curves using multi-source integration into CSV.",
        "m12-tests",
    )
    assert phase1.confirmation is not None
    store = MemoryBronzeStore()
    bundle = build_offline_scientific_bundle(phase1.confirmation.contract, store)
    return _Chain(
        store=store,
        dataset_store=MemoryDatasetIRStore(),
        request=ScientificParsingRequest(
            artifact=bundle.artifact,
            subset=bundle.subset,
            policy=bundle.policy,
            runtime=bundle.runtime,
            requested_at=bundle.runtime.checked_at,
        ),
    )


@pytest.fixture(scope="module")
def result(chain: _Chain) -> ScientificParsingResult:
    return asyncio.run(
        ScientificParsingService(
            bronze_store=chain.store,
            dataset_store=chain.dataset_store,
        ).execute(chain.request)
    )


def test_fits_binary_table_preserves_values_metadata_and_missingness(
    chain: _Chain, result: ScientificParsingResult
) -> None:
    assert result.status is ScientificParsingStatus.SUCCEEDED
    assert result.metrics.input_byte_count == 8640
    assert result.metrics.hdu_count == 2
    assert result.metrics.source_row_count == 4
    assert result.metrics.selected_variable_count == 3
    assert result.metrics.materialized_cell_count == 12
    assert result.metrics.missing_value_count == 1
    assert result.metrics.transformation_count == 1
    assert result.metrics.model_attempt_count == result.metrics.network_attempt_count == 0
    dataset = chain.dataset_store.read(result.dataset_ref.artifact_sha256)
    assert dataset.format_metadata.hdu_name == "LIGHTCURVE"
    cards = {item.keyword: item.value for item in dataset.format_metadata.header_cards}
    assert cards["TSCAL2"] == "0.01"
    assert cards["TZERO2"] == "10.0"
    assert cards["TIMESYS"] == "UTC"
    variables = {item.name: item for item in dataset.variables}
    assert variables["MJD"].unit == "d"
    assert variables["MAG"].transformation.kind is TransformationKind.LINEAR_SCALE
    assert variables["MAG"].values[0].raw_value == "125"
    assert variables["MAG"].values[0].physical_value == "11.25"
    assert variables["MAG_ERR"].values[2].kind is ScalarKind.MISSING
    assert variables["MAG_ERR"].values[2].missing_reason == "non_finite"


def test_result_replays_from_cache_checkpoint_and_force_recompute(chain: _Chain) -> None:
    checkpoints = MemoryScientificCheckpointStore()
    service = ScientificParsingService(
        bronze_store=chain.store,
        dataset_store=chain.dataset_store,
        checkpoints=checkpoints,
    )
    first = asyncio.run(service.execute(chain.request))
    replay = asyncio.run(service.execute(chain.request))
    forced = asyncio.run(
        service.execute(chain.request.model_copy(update={"force_recompute": True}))
    )
    assert first == replay == forced
    resumed = ScientificParsingService(
        bronze_store=chain.store,
        dataset_store=chain.dataset_store,
        checkpoints=checkpoints,
    )
    assert asyncio.run(resumed.execute(chain.request)) == first


def test_request_and_contract_limits_fail_before_materialization(chain: _Chain) -> None:
    with pytest.raises(ValidationError):
        ScientificSubset.model_validate(
            {
                "hdu_index": 1,
                "variable_names": ("MJD",),
                "row_start": 4,
                "row_stop": 4,
            }
        )
    with pytest.raises(ValidationError):
        ScientificSubset.model_validate(
            {
                "hdu_index": 1,
                "variable_names": ("MJD",),
                "row_start": 0,
                "row_stop": 1,
                "unexpected": True,
            }
        )
    oversized = chain.request.policy.model_copy(update={"max_selected_rows": 2})
    with pytest.raises(ValidationError):
        ScientificParsingRequest(
            artifact=chain.request.artifact,
            subset=chain.request.subset,
            policy=oversized,
            runtime=chain.request.runtime,
            requested_at=chain.request.requested_at,
        )


@pytest.mark.parametrize(
    "subset,message",
    [
        (ScientificSubset(hdu_index=1, variable_names=("UNKNOWN",), row_stop=1), "variable"),
        (ScientificSubset(hdu_index=9, variable_names=("MJD",), row_stop=1), "HDU"),
        (ScientificSubset(hdu_index=1, variable_names=("MJD",), row_stop=99), "row range"),
    ],
)
def test_invalid_fits_selection_is_structured(
    chain: _Chain, subset: ScientificSubset, message: str
) -> None:
    request = chain.request.model_copy(update={"subset": subset})
    with pytest.raises(AppError) as captured:
        asyncio.run(ScientificParsingService(bronze_store=chain.store).execute(request))
    assert captured.value.code is ErrorCode.VALIDATION_FAILED
    assert message in captured.value.message


def test_non_table_hdu_and_corrupt_bytes_fail_closed(chain: _Chain) -> None:
    parser = FitsParser()
    with pytest.raises(AppError) as unsupported:
        parser.parse(
            chain.store.read(chain.request.artifact.byte_sha256),
            ScientificSubset(hdu_index=0, variable_names=("MJD",), row_stop=1),
            chain.request.policy,
        )
    assert unsupported.value.code is ErrorCode.VALIDATION_FAILED
    with pytest.raises(AppError) as corrupt:
        parser.parse(b"not-fits", chain.request.subset, chain.request.policy)
    assert corrupt.value.code is ErrorCode.VALIDATION_FAILED


def test_source_dataset_and_checkpoint_tampering_is_detected(
    chain: _Chain, result: ScientificParsingResult
) -> None:
    original = chain.store._objects[chain.request.artifact.byte_sha256]
    chain.store._objects[chain.request.artifact.byte_sha256] = original[:-1] + b"x"
    with pytest.raises(AppError) as source_error:
        asyncio.run(ScientificParsingService(bronze_store=chain.store).execute(chain.request))
    assert source_error.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR
    chain.store._objects[chain.request.artifact.byte_sha256] = original

    dataset_payload = chain.dataset_store._values[result.dataset_ref.artifact_sha256]
    chain.dataset_store._values[result.dataset_ref.artifact_sha256] = b"{}"
    with pytest.raises(AppError) as dataset_error:
        chain.dataset_store.read(result.dataset_ref.artifact_sha256)
    assert dataset_error.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR
    chain.dataset_store._values[result.dataset_ref.artifact_sha256] = dataset_payload

    checkpoints = MemoryScientificCheckpointStore()
    checkpoints.save(result)
    checkpoints._values[result.idempotency_key] = b"{}"
    with pytest.raises(AppError) as checkpoint_error:
        checkpoints.load(result.idempotency_key)
    assert checkpoint_error.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR


def test_plugin_registry_and_optional_dependency_fail_with_install_hint(
    chain: _Chain, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = PluginParserRegistry(())
    with pytest.raises(AppError) as unavailable:
        empty.resolve(chain.request.runtime)
    assert unavailable.value.code is ErrorCode.CONFIGURATION_ERROR
    assert unavailable.value.details["parser_id"] == "m12.fits"

    import scidatafusion.scientific_formats.fits as fits_module

    def missing(_: str) -> object:
        raise ImportError("missing optional dependency")

    monkeypatch.setattr(fits_module, "import_module", missing)
    with pytest.raises(AppError) as dependency:
        FitsParser()
    assert dependency.value.code is ErrorCode.CONFIGURATION_ERROR
    assert dependency.value.details == {"extra": "scientific", "parser_id": "m12.fits"}


def test_storage_and_checkpoint_reject_invalid_keys(chain: _Chain) -> None:
    with pytest.raises(AppError) as dataset_error:
        chain.dataset_store.read("bad")
    assert dataset_error.value.code is ErrorCode.INVALID_REQUEST
    with pytest.raises(AppError) as checkpoint_error:
        MemoryScientificCheckpointStore().load("bad")
    assert checkpoint_error.value.code is ErrorCode.INVALID_REQUEST


def test_fits_checksum_fixture_is_stable() -> None:
    from importlib import import_module

    from scidatafusion.scientific_formats.fixtures import build_synthetic_ia_fits

    content = build_synthetic_ia_fits()
    fits = import_module("astropy.io.fits")
    with fits.open(BytesIO(content), checksum=True) as hdus:
        assert hdus[0].verify_checksum() == 1
        assert hdus[1].verify_checksum() == 1
