"""M07 immutable artifact acquisition primitives."""

from scidatafusion.artifacts.archive import ExtractedArchiveMember, SafeArchiveInspector
from scidatafusion.artifacts.downloader import (
    DnsPinnedTransport,
    DownloadFailure,
    DownloadFetchResult,
    HostResolver,
    SafeDownloadClient,
    SystemHostResolver,
    sanitize_url_for_manifest,
)
from scidatafusion.artifacts.fixtures import (
    OfflineArtifactBundle,
    build_offline_ia_artifact_bundle,
)
from scidatafusion.artifacts.integrity import (
    calculate_acquisition_hash,
    calculate_artifact_download_input_hash,
    calculate_artifact_download_output_hash,
    calculate_artifact_manifest_hash,
    calculate_bronze_artifact_set_hash,
    calculate_bronze_object_metadata_hash,
    calculate_candidate_locator_hash,
    calculate_download_policy_hash,
    calculate_download_run_log_hash,
    calculate_download_runtime_hash,
    calculate_url_locator_hash,
    verify_artifact_download_integrity,
    verify_artifact_download_request_integrity,
)
from scidatafusion.artifacts.service import ArtifactDownloadService
from scidatafusion.artifacts.sniffer import ContentSniffer
from scidatafusion.artifacts.storage import (
    BronzeByteStore,
    BronzeWriteReceipt,
    FileSystemBronzeStore,
    MemoryBronzeStore,
)

__all__ = [
    "ArtifactDownloadService",
    "BronzeByteStore",
    "BronzeWriteReceipt",
    "ContentSniffer",
    "DnsPinnedTransport",
    "DownloadFailure",
    "DownloadFetchResult",
    "ExtractedArchiveMember",
    "FileSystemBronzeStore",
    "HostResolver",
    "MemoryBronzeStore",
    "OfflineArtifactBundle",
    "SafeArchiveInspector",
    "SafeDownloadClient",
    "SystemHostResolver",
    "build_offline_ia_artifact_bundle",
    "calculate_acquisition_hash",
    "calculate_artifact_download_input_hash",
    "calculate_artifact_download_output_hash",
    "calculate_artifact_manifest_hash",
    "calculate_bronze_artifact_set_hash",
    "calculate_bronze_object_metadata_hash",
    "calculate_candidate_locator_hash",
    "calculate_download_policy_hash",
    "calculate_download_run_log_hash",
    "calculate_download_runtime_hash",
    "calculate_url_locator_hash",
    "sanitize_url_for_manifest",
    "verify_artifact_download_integrity",
    "verify_artifact_download_request_integrity",
]
