"""Controlled, replayable Connector execution for federated scientific search."""

from scidatafusion.connectors.base import Connector, ConnectorExecutionOutcome
from scidatafusion.connectors.executor import ConnectorBatchExecutor
from scidatafusion.connectors.fixtures import (
    OfflineConnectorBundle,
    build_offline_ia_connector_bundle,
)
from scidatafusion.connectors.http import ControlledHttpConnector
from scidatafusion.connectors.integrity import (
    calculate_candidate_set_hash,
    calculate_connector_output_hash,
    calculate_evidence_set_hash,
    calculate_run_log_hash,
    calculate_source_candidate_hash,
    verify_connector_execution_integrity,
)
from scidatafusion.connectors.normalizer import ObservedRecord, normalize_candidates
from scidatafusion.connectors.registry import (
    ConnectorRegistryLoader,
    calculate_connector_descriptor_hash,
    find_connector_by_id,
    find_connector_by_source,
    load_connector_registry,
    load_default_connector_registry,
    require_connector_by_id,
    require_connector_by_source,
)

__all__ = [
    "Connector",
    "ConnectorBatchExecutor",
    "ConnectorExecutionOutcome",
    "ConnectorRegistryLoader",
    "ControlledHttpConnector",
    "ObservedRecord",
    "OfflineConnectorBundle",
    "build_offline_ia_connector_bundle",
    "calculate_candidate_set_hash",
    "calculate_connector_descriptor_hash",
    "calculate_connector_output_hash",
    "calculate_evidence_set_hash",
    "calculate_run_log_hash",
    "calculate_source_candidate_hash",
    "find_connector_by_id",
    "find_connector_by_source",
    "load_connector_registry",
    "load_default_connector_registry",
    "normalize_candidates",
    "require_connector_by_id",
    "require_connector_by_source",
    "verify_connector_execution_integrity",
]
