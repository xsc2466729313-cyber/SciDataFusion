"""Content-addressed FITS light-curve fixture for the M12 offline slice."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from importlib import import_module
from io import BytesIO

from scidatafusion.artifacts.storage import MemoryBronzeStore
from scidatafusion.contracts.base import utc_now
from scidatafusion.contracts.datasets import (
    ScientificArtifact,
    ScientificExecutionMode,
    ScientificFormat,
    ScientificParserDescriptor,
    ScientificParsingPolicy,
    ScientificRuntimeSnapshot,
    ScientificSubset,
)
from scidatafusion.contracts.scientific import ScientificDataContract
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.parsing.registry import load_default_parser_registry
from scidatafusion.scientific_formats.fits import FitsParser
from scidatafusion.scientific_formats.integrity import (
    calculate_scientific_artifact_hash,
    calculate_scientific_descriptor_hash,
    calculate_scientific_runtime_hash,
)


@dataclass(frozen=True, slots=True)
class OfflineScientificBundle:
    artifact: ScientificArtifact
    subset: ScientificSubset
    policy: ScientificParsingPolicy
    runtime: ScientificRuntimeSnapshot


def build_offline_scientific_bundle(
    contract: ScientificDataContract,
    store: MemoryBronzeStore,
    *,
    clock: Callable[[], datetime] = utc_now,
) -> OfflineScientificBundle:
    """Create a bounded Ia FITS table and an immutable M08 route projection."""

    created_at = clock()
    content = build_synthetic_ia_fits()
    receipt = store.put(content)
    registry = load_default_parser_registry()
    capability = next(item for item in registry.parsers if item.parser_id == "m12.fits")
    parser = FitsParser()
    descriptor_draft = ScientificParserDescriptor(
        parser_id=parser.parser_id,
        parser_version=parser.parser_version,
        capability_hash=capability.capability_hash,
        engine_name=parser.engine_name,
        engine_version=parser.engine_version,
        supported_format=ScientificFormat.FITS,
        descriptor_hash="0" * 64,
    )
    descriptor = descriptor_draft.model_copy(
        update={"descriptor_hash": calculate_scientific_descriptor_hash(descriptor_draft)}
    )
    runtime_draft = ScientificRuntimeSnapshot(
        execution_mode=ScientificExecutionMode.OFFLINE,
        capability_registry_hash=registry.registry_hash,
        parser=descriptor,
        checked_at=created_at,
        runtime_hash="0" * 64,
    )
    runtime = runtime_draft.model_copy(
        update={"runtime_hash": calculate_scientific_runtime_hash(runtime_draft)}
    )
    route_hash = canonical_hash(
        {
            "object_id": f"brz_{receipt.byte_sha256[:32]}",
            "parser_id": parser.parser_id,
            "parser_version": parser.parser_version,
            "capability_registry_hash": registry.registry_hash,
            "target_module": "M12",
        }
    )
    plan_hash = canonical_hash(
        {"contract_id": contract.contract_id, "route_hash": route_hash, "module": "M08"}
    )
    artifact_draft = ScientificArtifact(
        task_id=contract.task_id,
        run_id=contract.run_id,
        contract_version=contract.version,
        contract_id=contract.contract_id,
        parse_plan_id=f"ppl_{plan_hash[:32]}",
        route_id=f"prt_{route_hash[:32]}",
        route_hash=route_hash,
        capability_registry_hash=registry.registry_hash,
        object_id=f"brz_{receipt.byte_sha256[:32]}",
        byte_sha256=receipt.byte_sha256,
        size_bytes=receipt.size_bytes,
        media_type="application/fits",
        format=ScientificFormat.FITS,
        parser_id="m12.fits",
        parser_version="1.0.0",
        artifact_hash="0" * 64,
    )
    artifact = artifact_draft.model_copy(
        update={"artifact_hash": calculate_scientific_artifact_hash(artifact_draft)}
    )
    return OfflineScientificBundle(
        artifact=artifact,
        subset=ScientificSubset(
            hdu_index=1,
            variable_names=("MJD", "MAG", "MAG_ERR"),
            row_start=0,
            row_stop=4,
        ),
        policy=ScientificParsingPolicy(),
        runtime=runtime,
    )


def build_synthetic_ia_fits() -> bytes:
    """Build four deterministic light-curve rows, including scaling and one missing value."""

    fits = import_module("astropy.io.fits")
    numpy = import_module("numpy")
    columns = [
        fits.Column(
            name="MJD",
            format="D",
            unit="d",
            array=numpy.array([59000.0, 59001.5, 59003.0, 59004.5], dtype="float64"),
        ),
        fits.Column(
            name="MAG",
            format="I",
            unit="mag",
            array=numpy.array([125, 150, 200, 275], dtype="int16"),
        ),
        fits.Column(
            name="MAG_ERR",
            format="E",
            unit="mag",
            array=numpy.array([0.05, 0.04, float("nan"), 0.08], dtype="float32"),
        ),
    ]
    table = fits.BinTableHDU.from_columns(columns, name="LIGHTCURVE")
    table.header["TSCAL2"] = (0.01, "physical magnitude scale")
    table.header["TZERO2"] = (10.0, "physical magnitude zero point")
    table.header["TIMESYS"] = ("UTC", "time scale retained as format metadata")
    output = BytesIO()
    fits.HDUList([fits.PrimaryHDU(), table]).writeto(output, checksum=True)
    return output.getvalue()
