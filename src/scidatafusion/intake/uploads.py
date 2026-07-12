"""Upload metadata checks that never expand untrusted archives."""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePath

from scidatafusion.contracts.base import RunId, SemanticVersion, TaskId
from scidatafusion.contracts.task import (
    InputArtifactManifest,
    InputArtifactRecord,
    InputArtifactRequest,
    IntakeProblem,
    IntakeProblemCode,
    ProblemDetail,
)

ALLOWED_EXTENSIONS: dict[str, frozenset[str]] = {
    "application/fits": frozenset({".fit", ".fits", ".fts"}),
    "application/gzip": frozenset({".gz"}),
    "application/json": frozenset({".json"}),
    "application/pdf": frozenset({".pdf"}),
    "application/zip": frozenset({".zip"}),
    "image/fits": frozenset({".fit", ".fits", ".fts"}),
    "image/jpeg": frozenset({".jpeg", ".jpg"}),
    "image/png": frozenset({".png"}),
    "text/csv": frozenset({".csv"}),
    "text/plain": frozenset({".txt"}),
    "text/tab-separated-values": frozenset({".tsv"}),
}
ARCHIVE_MEDIA_TYPES = frozenset({"application/gzip", "application/zip"})


class UploadManifestBuilder:
    """Validate declared upload metadata and quarantine any unsafe artifact."""

    def __init__(
        self,
        *,
        max_file_bytes: int = 100_000_000,
        max_total_bytes: int = 500_000_000,
        max_expanded_bytes: int = 1_000_000_000,
        max_compression_ratio: float = 100.0,
        max_archive_entries: int = 10_000,
        policy_version: SemanticVersion = "1.0.0",
    ) -> None:
        if min(max_file_bytes, max_total_bytes, max_expanded_bytes, max_archive_entries) <= 0:
            msg = "upload size and entry limits must be positive"
            raise ValueError(msg)
        if max_compression_ratio < 1.0:
            msg = "max_compression_ratio must be at least 1.0"
            raise ValueError(msg)
        self._max_file_bytes = max_file_bytes
        self._max_total_bytes = max_total_bytes
        self._max_expanded_bytes = max_expanded_bytes
        self._max_compression_ratio = max_compression_ratio
        self._max_archive_entries = max_archive_entries
        self._policy_version = policy_version

    def configuration(self) -> tuple[tuple[str, str], ...]:
        """Return a stable, non-secret representation for configuration hashing."""

        return (
            ("max_file_bytes", str(self._max_file_bytes)),
            ("max_total_bytes", str(self._max_total_bytes)),
            ("max_expanded_bytes", str(self._max_expanded_bytes)),
            ("max_compression_ratio", str(self._max_compression_ratio)),
            ("max_archive_entries", str(self._max_archive_entries)),
            ("policy_version", self._policy_version),
        )

    def build(
        self,
        uploads: tuple[InputArtifactRequest, ...],
        *,
        task_id: TaskId,
        run_id: RunId,
        contract_version: SemanticVersion,
        producer_version: SemanticVersion,
        created_at: datetime,
    ) -> InputArtifactManifest:
        """Build a manifest using metadata only; archive bytes are never decompressed."""

        all_problems: list[IntakeProblem] = []
        records: list[InputArtifactRecord] = []
        total_size = sum(upload.size_bytes for upload in uploads)
        total_expanded_size = sum(self._expansion(upload)[0] for upload in uploads)

        global_problems: list[IntakeProblem] = []
        if total_size > self._max_total_bytes:
            global_problems.append(
                IntakeProblem(
                    code=IntakeProblemCode.UPLOAD_TOTAL_TOO_LARGE,
                    message="Combined upload size exceeds the configured limit",
                    field="input_artifacts",
                    details=(
                        ProblemDetail(key="actual_bytes", value=str(total_size)),
                        ProblemDetail(key="limit_bytes", value=str(self._max_total_bytes)),
                    ),
                )
            )
        if total_expanded_size > self._max_expanded_bytes:
            global_problems.append(
                IntakeProblem(
                    code=IntakeProblemCode.ARCHIVE_EXPANSION_LIMIT_EXCEEDED,
                    message="Combined expanded upload size exceeds the configured limit",
                    field="input_artifacts",
                    details=(
                        ProblemDetail(key="actual_bytes", value=str(total_expanded_size)),
                        ProblemDetail(key="limit_bytes", value=str(self._max_expanded_bytes)),
                    ),
                )
            )
        all_problems.extend(global_problems)

        for upload in uploads:
            problems = list(self._validate_upload(upload))
            all_problems.extend(problems)
            expanded_size, ratio = self._expansion(upload)
            records.append(
                InputArtifactRecord(
                    filename=upload.filename,
                    uri=upload.uri,
                    sha256=upload.sha256,
                    media_type=upload.media_type.lower(),
                    size_bytes=upload.size_bytes,
                    expanded_size_bytes=expanded_size,
                    compression_ratio=ratio,
                    archive_entry_count=upload.archive_entry_count,
                    quarantined=bool(problems or global_problems),
                )
            )

        return InputArtifactManifest(
            task_id=task_id,
            run_id=run_id,
            contract_version=contract_version,
            producer_version=producer_version,
            created_at=created_at,
            artifacts=tuple(records),
            total_size_bytes=total_size,
            total_expanded_size_bytes=total_expanded_size,
            validated=not all_problems,
            problems=tuple(all_problems),
            policy_version=self._policy_version,
        )

    def _validate_upload(self, upload: InputArtifactRequest) -> tuple[IntakeProblem, ...]:
        problems: list[IntakeProblem] = []
        media_type = upload.media_type.lower()
        suffix = PurePath(upload.filename).suffix.lower()
        allowed_extensions = ALLOWED_EXTENSIONS.get(media_type)

        if allowed_extensions is None:
            problems.append(
                self._problem(
                    IntakeProblemCode.MEDIA_TYPE_BLOCKED,
                    "Upload media type is not allowed",
                    upload,
                    media_type=media_type,
                )
            )
        elif suffix not in allowed_extensions:
            problems.append(
                self._problem(
                    IntakeProblemCode.FILE_EXTENSION_MISMATCH,
                    "Filename extension does not match the declared media type",
                    upload,
                    extension=suffix or "<none>",
                    media_type=media_type,
                )
            )

        if upload.size_bytes > self._max_file_bytes:
            problems.append(
                self._problem(
                    IntakeProblemCode.UPLOAD_TOO_LARGE,
                    "Upload exceeds the per-file size limit",
                    upload,
                    actual_bytes=str(upload.size_bytes),
                    limit_bytes=str(self._max_file_bytes),
                )
            )

        if media_type in ARCHIVE_MEDIA_TYPES:
            if upload.expanded_size_bytes is None or upload.archive_entry_count is None:
                problems.append(
                    self._problem(
                        IntakeProblemCode.ARCHIVE_METADATA_REQUIRED,
                        "Archive directory metadata is required before parsing",
                        upload,
                    )
                )
            else:
                expanded_size, ratio = self._expansion(upload)
                if upload.archive_entry_count > self._max_archive_entries:
                    problems.append(
                        self._problem(
                            IntakeProblemCode.ARCHIVE_ENTRY_LIMIT_EXCEEDED,
                            "Archive entry count exceeds the configured limit",
                            upload,
                            actual_entries=str(upload.archive_entry_count),
                            limit_entries=str(self._max_archive_entries),
                        )
                    )
                if expanded_size > self._max_expanded_bytes:
                    problems.append(
                        self._problem(
                            IntakeProblemCode.ARCHIVE_EXPANSION_LIMIT_EXCEEDED,
                            "Archive expanded size exceeds the configured limit",
                            upload,
                            actual_bytes=str(expanded_size),
                            limit_bytes=str(self._max_expanded_bytes),
                        )
                    )
                if ratio > self._max_compression_ratio:
                    problems.append(
                        self._problem(
                            IntakeProblemCode.COMPRESSION_RATIO_EXCEEDED,
                            "Archive compression ratio exceeds the configured limit",
                            upload,
                            actual_ratio=str(ratio),
                            limit_ratio=str(self._max_compression_ratio),
                        )
                    )
        return tuple(problems)

    @staticmethod
    def _expansion(upload: InputArtifactRequest) -> tuple[int, float]:
        if upload.media_type.lower() not in ARCHIVE_MEDIA_TYPES:
            return upload.size_bytes, 1.0
        expanded_size = (
            upload.size_bytes if upload.expanded_size_bytes is None else upload.expanded_size_bytes
        )
        if upload.size_bytes == 0:
            ratio = 1.0 if expanded_size == 0 else 1e308
        else:
            ratio = max(1.0, expanded_size / upload.size_bytes)
        return expanded_size, ratio

    @staticmethod
    def _problem(
        code: IntakeProblemCode,
        message: str,
        upload: InputArtifactRequest,
        **details: str,
    ) -> IntakeProblem:
        return IntakeProblem(
            code=code,
            message=message,
            field="input_artifacts",
            details=(
                ProblemDetail(key="filename", value=upload.filename),
                *(ProblemDetail(key=key, value=value) for key, value in sorted(details.items())),
            ),
        )
