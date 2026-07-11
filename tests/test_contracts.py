from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from scidatafusion.contracts import (
    ArtifactReference,
    EventEnvelope,
    EventType,
    NonEmptyStr,
    ProducerRef,
    StrictContract,
    generate_id,
)


class TaskCreatedPayload(StrictContract):
    research_goal: NonEmptyStr


def test_prefixed_ids_are_unique() -> None:
    task_ids = {generate_id("tsk") for _ in range(20)}

    assert len(task_ids) == 20
    assert all(identifier.startswith("tsk_") and len(identifier) == 36 for identifier in task_ids)


def test_contracts_forbid_unknown_fields_and_are_frozen() -> None:
    payload = TaskCreatedPayload(research_goal="  Study Ia supernovae  ")

    assert payload.research_goal == "Study Ia supernovae"
    with pytest.raises(ValidationError):
        TaskCreatedPayload(research_goal="goal", unexpected=True)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        payload.research_goal = "changed"  # type: ignore[misc]


def test_event_envelope_round_trip() -> None:
    event = EventEnvelope[TaskCreatedPayload](
        event_type=EventType.TASK_CREATED,
        task_id=generate_id("tsk"),
        run_id=generate_id("run"),
        producer=ProducerRef(component="intake", version="1.0.0"),
        payload=TaskCreatedPayload(research_goal="Study Ia supernova light curves"),
    )

    restored = EventEnvelope[TaskCreatedPayload].model_validate_json(event.model_dump_json())

    assert restored == event
    assert restored.occurred_at.tzinfo is not None
    assert restored.event_id.startswith("evt_")


def test_event_rejects_naive_timestamps_and_invalid_versions() -> None:
    task_id = generate_id("tsk")
    run_id = generate_id("run")
    payload = TaskCreatedPayload(research_goal="goal")

    with pytest.raises(ValidationError, match="timezone"):
        EventEnvelope[TaskCreatedPayload](
            event_type=EventType.TASK_CREATED,
            task_id=task_id,
            run_id=run_id,
            payload=payload,
            occurred_at=datetime(2026, 7, 11),  # noqa: DTZ001 - intentional invalid input
            producer=ProducerRef(component="intake", version="1.0.0"),
        )
    with pytest.raises(ValidationError):
        ProducerRef(component="intake", version="latest")


def test_aware_timestamp_is_normalized_to_utc() -> None:
    event = EventEnvelope[TaskCreatedPayload](
        event_type=EventType.TASK_CREATED,
        task_id=generate_id("tsk"),
        run_id=generate_id("run"),
        occurred_at=datetime.now(UTC),
        producer=ProducerRef(component="intake", version="1.0.0"),
        payload=TaskCreatedPayload(research_goal="goal"),
    )

    assert event.occurred_at.tzinfo is UTC


def test_artifact_reference_requires_content_hash_and_aware_time() -> None:
    artifact = ArtifactReference(
        uri="file:///var/bronze/example.pdf",
        sha256="a" * 64,
        media_type="application/pdf",
        size_bytes=42,
    )

    assert artifact.artifact_id.startswith("art_")
    assert artifact.created_at.tzinfo is UTC
    with pytest.raises(ValidationError):
        ArtifactReference(
            uri="file:///tmp/x",
            sha256="not-a-hash",
            media_type="text/plain",
            size_bytes=1,
        )
    with pytest.raises(ValidationError):
        ArtifactReference(
            uri="file:///tmp/x",
            sha256="b" * 64,
            media_type="text/plain",
            size_bytes="1",  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError, match="timezone"):
        ArtifactReference(
            uri="file:///tmp/x",
            sha256="b" * 64,
            media_type="text/plain",
            size_bytes=1,
            created_at=datetime(2026, 7, 11),  # noqa: DTZ001 - intentional invalid input
        )
