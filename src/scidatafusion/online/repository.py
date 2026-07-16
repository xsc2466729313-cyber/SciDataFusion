"""DuckDB catalog for immutable locally acquired online artifacts."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import duckdb

from scidatafusion.contracts.online import (
    AgentReflectionRound,
    OnlineAcquisitionResult,
    OnlineArtifactCatalogSnapshot,
)


class DuckDBOnlineArtifactRepository:
    """Persist artifact metadata and append-only acquisition facts in DuckDB."""

    def __init__(self, path: Path = Path("var/online_artifacts.duckdb")) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path.resolve()

    def persist(self, result: OnlineAcquisitionResult) -> OnlineArtifactCatalogSnapshot:
        with duckdb.connect(str(self._path)) as connection:
            self._initialize(connection)
            connection.begin()
            try:
                now = datetime.now(UTC)
                for artifact in result.artifacts:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO online_artifacts VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            artifact.byte_sha256,
                            artifact.size_bytes,
                            artifact.media_type,
                            artifact.artifact_kind,
                            artifact.storage_uri,
                            now,
                        ),
                    )
                    event_id = self._event_id(artifact.locator_hash, artifact.byte_sha256, "stored")
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO online_acquisition_events
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            artifact.locator_hash,
                            artifact.byte_sha256,
                            self._sanitized_url(str(artifact.source_url)),
                            artifact.source_title,
                            "stored",
                            now,
                        ),
                    )
                for failure in result.failures:
                    event_id = self._event_id(failure.locator_hash, failure.error_code, "failed")
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO online_acquisition_failures
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            failure.locator_hash,
                            self._sanitized_url(str(failure.source_url)),
                            failure.source_title,
                            failure.error_code,
                            failure.retryable,
                            now,
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            artifact_summary = connection.execute(
                "SELECT count(*), coalesce(sum(size_bytes), 0) FROM online_artifacts"
            ).fetchone()
            acquisition_summary = connection.execute(
                "SELECT count(*) FROM online_acquisition_events"
            ).fetchone()
            failure_summary = connection.execute(
                "SELECT count(*) FROM online_acquisition_failures"
            ).fetchone()
            if artifact_summary is None or acquisition_summary is None or failure_summary is None:
                raise RuntimeError("DuckDB catalog summary query returned no row")
            artifact_count, stored_bytes = artifact_summary
            acquisition_count = acquisition_summary[0]
            failure_count = failure_summary[0]
        return OnlineArtifactCatalogSnapshot(
            database_path=str(self._path),
            artifact_count=artifact_count,
            acquisition_event_count=acquisition_count,
            failure_event_count=failure_count,
            stored_byte_count=stored_bytes,
        )

    def persist_reflection_round(self, reflection: AgentReflectionRound) -> None:
        """Checkpoint one immutable reflection decision for automatic continuation."""

        with duckdb.connect(str(self._path)) as connection:
            self._initialize(connection)
            connection.execute(
                """
                INSERT OR IGNORE INTO online_reflection_events
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reflection.proof_hash,
                    reflection.iteration,
                    reflection.input_query,
                    ",".join(reflection.gaps),
                    reflection.decision,
                    reflection.reflection_strategy,
                    reflection.reflection_summary,
                    reflection.next_query,
                    reflection.acquired_artifact_count,
                    reflection.useful_artifact_count,
                    datetime.now(UTC),
                ),
            )
            for qualification in reflection.qualifications:
                qualification_id = self._event_id(
                    reflection.proof_hash,
                    qualification.byte_sha256,
                    "qualified",
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO online_artifact_qualifications
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        qualification_id,
                        reflection.proof_hash,
                        qualification.byte_sha256,
                        qualification.relevant_to_goal,
                        qualification.contains_scientific_records,
                        qualification.confidence,
                        qualification.accepted,
                        qualification.rationale,
                        (
                            None
                            if reflection.qualification_model_invocation is None
                            else reflection.qualification_model_invocation.response_hash
                        ),
                        datetime.now(UTC),
                    ),
                )

    @staticmethod
    def _initialize(connection: duckdb.DuckDBPyConnection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS online_artifacts (
                byte_sha256 VARCHAR PRIMARY KEY,
                size_bytes UBIGINT NOT NULL,
                media_type VARCHAR NOT NULL,
                artifact_kind VARCHAR NOT NULL,
                storage_uri VARCHAR NOT NULL,
                first_seen_at TIMESTAMPTZ NOT NULL
            );
            CREATE TABLE IF NOT EXISTS online_acquisition_events (
                event_id VARCHAR PRIMARY KEY,
                locator_hash VARCHAR NOT NULL,
                byte_sha256 VARCHAR NOT NULL,
                source_url VARCHAR NOT NULL,
                source_title VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            );
            CREATE TABLE IF NOT EXISTS online_acquisition_failures (
                event_id VARCHAR PRIMARY KEY,
                locator_hash VARCHAR NOT NULL,
                source_url VARCHAR NOT NULL,
                source_title VARCHAR NOT NULL,
                error_code VARCHAR NOT NULL,
                retryable BOOLEAN NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            );
            CREATE TABLE IF NOT EXISTS online_reflection_events (
                proof_hash VARCHAR PRIMARY KEY,
                iteration UTINYINT NOT NULL,
                input_query VARCHAR NOT NULL,
                gaps VARCHAR NOT NULL,
                decision VARCHAR NOT NULL,
                reflection_strategy VARCHAR NOT NULL,
                reflection_summary VARCHAR NOT NULL,
                next_query VARCHAR,
                acquired_artifact_count UTINYINT NOT NULL,
                useful_artifact_count UTINYINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            );
            CREATE TABLE IF NOT EXISTS online_artifact_qualifications (
                qualification_id VARCHAR PRIMARY KEY,
                reflection_proof_hash VARCHAR NOT NULL,
                byte_sha256 VARCHAR NOT NULL,
                relevant_to_goal BOOLEAN NOT NULL,
                contains_scientific_records BOOLEAN NOT NULL,
                confidence DOUBLE NOT NULL,
                accepted BOOLEAN NOT NULL,
                rationale VARCHAR NOT NULL,
                model_response_hash VARCHAR,
                created_at TIMESTAMPTZ NOT NULL
            )
            """
        )

    @staticmethod
    def _event_id(locator_hash: str, outcome: str, status: str) -> str:
        return hashlib.sha256(f"{locator_hash}:{outcome}:{status}".encode()).hexdigest()

    @staticmethod
    def _sanitized_url(url: str) -> str:
        parsed = urlsplit(url)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
