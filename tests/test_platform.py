"""M26 deployable AI platform contracts and adapters."""

from __future__ import annotations

import asyncio
import importlib
import json
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from scidatafusion.api import DemoDeliveryProvider, create_app
from scidatafusion.config import Settings
from scidatafusion.contracts.platform import (
    EvidenceVectorDocument,
    ResearchJobRecord,
    ResearchJobResult,
    ResearchJobStatus,
    ResearchJobSubmission,
)
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.platform.agent_graph import BoundedResearchGraph
from scidatafusion.platform.jobs import (
    CeleryJobDispatcher,
    InMemoryResearchJobRepository,
    PostgresResearchJobRepository,
    ResearchJobService,
)
from scidatafusion.platform.status import _module_available, build_platform_status
from scidatafusion.platform.vectors import (
    ChromaEvidenceIndex,
    EvidenceVectorizer,
    _framework_document_counts,
    build_evidence_documents,
)


def _submission(key: str | None = None) -> ResearchJobSubmission:
    return ResearchJobSubmission(
        research_goal="研究 Ia 型超新星光变曲线并整合开放科学数据",
        retrieval_query="light curve photometry data",
        idempotency_key=key,
    )


def _result() -> ResearchJobResult:
    return ResearchJobResult(
        task_id="tsk_demo",
        run_id="run_demo",
        quality_gate_passed=True,
        quality_score=1.0,
        source_count=3,
        evidence_count=76,
        artifact_count=7,
        issue_count=0,
        formal_gold_record_count=8,
        package_filename="result.zip",
    )


async def _wait_terminal(service: ResearchJobService, job_id: str) -> ResearchJobRecord:
    for _ in range(100):
        record = await service.get(job_id)
        assert record is not None
        if record.status in {ResearchJobStatus.SUCCEEDED, ResearchJobStatus.FAILED}:
            return record
        await asyncio.sleep(0)
    raise AssertionError("job did not finish")


def test_platform_contracts_are_strict_and_time_aware() -> None:
    with pytest.raises(ValidationError):
        ResearchJobSubmission.model_validate(
            {"research_goal": "研究一个足够长的科学问题", "unexpected": True}
        )
    with pytest.raises(ValidationError):
        ResearchJobRecord.model_validate(
            {"submission": _submission(), "submitted_at": "2026-01-01T00:00:00"}
        )


def test_memory_repository_is_idempotent_and_ordered() -> None:
    async def exercise() -> None:
        repository = InMemoryResearchJobRepository()
        first = await repository.create(ResearchJobRecord(submission=_submission("stable-key")))
        duplicate = await repository.create(ResearchJobRecord(submission=_submission("stable-key")))
        second = await repository.create(ResearchJobRecord(submission=_submission()))
        assert duplicate.job_id == first.job_id
        assert await repository.get("job_" + "0" * 32) is None
        assert {item.job_id for item in await repository.list(2)} == {second.job_id, first.job_id}
        replacement = first.model_copy(update={"status": ResearchJobStatus.RUNNING})
        await repository.replace(replacement)
        assert (await repository.get(first.job_id)) == replacement
        with pytest.raises(KeyError):
            await repository.replace(ResearchJobRecord(submission=_submission()))

    asyncio.run(exercise())


def test_local_job_service_succeeds_fails_and_ignores_reexecution() -> None:
    async def exercise() -> None:
        repository = InMemoryResearchJobRepository()

        async def execute(submission: ResearchJobSubmission) -> ResearchJobResult:
            if "失败" in submission.research_goal:
                raise RuntimeError("secret provider details")
            return _result()

        service = ResearchJobService(repository, execute)
        succeeded = await service.submit(_submission("success-key"))
        duplicate = await service.submit(_submission("success-key"))
        assert duplicate.job_id == succeeded.job_id
        terminal = await _wait_terminal(service, succeeded.job_id)
        assert terminal.status is ResearchJobStatus.SUCCEEDED
        assert terminal.result == _result()
        assert terminal.started_at is not None and terminal.finished_at is not None
        await service.execute(succeeded.job_id)

        failed = await service.submit(
            ResearchJobSubmission(research_goal="这是一个预期失败的研究任务用于边界测试")
        )
        failure = await _wait_terminal(service, failed.job_id)
        assert failure.status is ResearchJobStatus.FAILED
        assert failure.failure_code == "research_execution_failed"
        assert failure.failure_message == "研究流程在处理来源或数据时未能完成, 已保留失败检查点。"
        assert failure.recovery_action is not None
        assert "secret" not in failure.model_dump_json()
        page = await service.list(1)
        assert page.count == 1

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (
            AppError(
                ErrorCode.CONFIGURATION_ERROR,
                "online research configuration is incomplete: secret material",
            ),
            "online_configuration_incomplete",
        ),
        (
            AppError(ErrorCode.EXTERNAL_SERVICE_ERROR, "provider detail"),
            "external_service_unavailable",
        ),
        (AppError(ErrorCode.BUDGET_EXCEEDED, "budget detail"), "research_budget_exceeded"),
        (
            AppError(ErrorCode.SECURITY_POLICY_VIOLATION, "policy detail"),
            "security_policy_blocked",
        ),
    ],
)
def test_job_failures_expose_safe_chinese_guidance(error: AppError, expected_code: str) -> None:
    async def exercise() -> None:
        async def fail(_: ResearchJobSubmission) -> ResearchJobResult:
            raise error

        service = ResearchJobService(InMemoryResearchJobRepository(), fail)
        submitted = await service.submit(_submission())
        failed = await _wait_terminal(service, submitted.job_id)
        assert failed.failure_code == expected_code
        assert failed.failure_message is not None
        assert failed.recovery_action is not None
        assert "detail" not in failed.model_dump_json()

    asyncio.run(exercise())


class _FakeConnection:
    def __init__(self) -> None:
        self.records: dict[str, str] = {}
        self.keys: dict[str, str] = {}
        self.closed = False

    async def execute(self, query: str, *args: object) -> str:
        if query.lstrip().startswith("CREATE"):
            return "CREATE TABLE"
        if query.startswith("UPDATE"):
            job_id, payload = str(args[0]), str(args[1])
            if job_id not in self.records:
                return "UPDATE 0"
            self.records[job_id] = payload
            return "UPDATE 1"
        raise AssertionError(query)

    async def fetchrow(self, query: str, *args: object) -> tuple[str] | None:
        if query.lstrip().startswith("INSERT"):
            job_id, key, _, payload = str(args[0]), args[1], args[2], str(args[3])
            if key is not None and str(key) in self.keys:
                return None
            self.records[job_id] = payload
            if key is not None:
                self.keys[str(key)] = job_id
            return (payload,)
        if "idempotency_key" in query:
            existing_job_id = self.keys.get(str(args[0]))
            return None if existing_job_id is None else (self.records[existing_job_id],)
        stored_payload = self.records.get(str(args[0]))
        return None if stored_payload is None else (stored_payload,)

    async def fetch(self, query: str, limit: int) -> list[tuple[str]]:
        assert "$1" in query
        return [(payload,) for payload in list(self.records.values())[-limit:]]

    async def close(self) -> None:
        self.closed = True


def test_postgres_repository_uses_parameterized_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    async def exercise() -> None:
        connection = _FakeConnection()
        repository = PostgresResearchJobRepository("postgresql://ignored")

        async def connect() -> _FakeConnection:
            connection.closed = False
            return connection

        monkeypatch.setattr(repository, "_connect", connect)
        first = await repository.create(ResearchJobRecord(submission=_submission("db-key-001")))
        duplicate = await repository.create(ResearchJobRecord(submission=_submission("db-key-001")))
        assert duplicate.job_id == first.job_id
        assert await repository.get(first.job_id) == first
        running = first.model_copy(update={"status": ResearchJobStatus.RUNNING})
        await repository.replace(running)
        assert (await repository.list(10))[0] == running
        with pytest.raises(KeyError):
            await repository.replace(ResearchJobRecord(submission=_submission()))
        assert connection.closed is True

    asyncio.run(exercise())


def test_celery_dispatcher_sends_validated_secret_free_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, list[object]]] = []

    class FakeCelery:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["broker"] == "redis://queue"

        def send_task(self, name: str, *, args: list[object]) -> None:
            calls.append((name, args))

    original = importlib.import_module

    def import_module(name: str) -> Any:
        if name == "celery":
            return type("CeleryModule", (), {"Celery": FakeCelery})
        return original(name)

    monkeypatch.setattr(importlib, "import_module", import_module)

    async def exercise() -> None:
        dispatcher = CeleryJobDispatcher("redis://queue")
        record = ResearchJobRecord(submission=_submission())
        await dispatcher.dispatch(record)

    asyncio.run(exercise())
    assert calls[0][0] == "scidatafusion.execute_research_job"
    assert str(calls[0][1][0]).startswith("job_")
    assert "api_key" not in json.dumps(calls)


def test_hash_vectorizer_is_deterministic_and_finite(monkeypatch: pytest.MonkeyPatch) -> None:
    original = importlib.import_module

    def no_optional(name: str) -> Any:
        if name.startswith(("sklearn", "torch")):
            raise ModuleNotFoundError(name)
        return original(name)

    monkeypatch.setattr(importlib, "import_module", no_optional)
    vectorizer = EvidenceVectorizer(64)
    first = vectorizer.encode(["magnitude 15.2 evidence"])[0]
    second = vectorizer.encode(["magnitude 15.2 evidence"])[0]
    assert first == second
    assert len(first) == 64
    assert all(value == value for value in first)
    assert vectorizer.engine == "python-hashing"
    assert vectorizer.torch_available() is False


def test_chroma_index_upserts_evidence_with_framework_views(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upserts: list[dict[str, object]] = []

    class Collection:
        def upsert(self, **kwargs: object) -> None:
            upserts.append(kwargs)

    class Chroma:
        @staticmethod
        def HttpClient(**kwargs: object) -> Any:
            assert kwargs == {"host": "chroma", "port": 8000, "ssl": False}
            return type(
                "Client", (), {"get_or_create_collection": lambda self, **_: Collection()}
            )()

    class Document:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    class TextNode(Document):
        pass

    original = importlib.import_module

    def fake_import(name: str) -> Any:
        if name == "chromadb":
            return Chroma
        if name == "langchain_core.documents":
            return type("Docs", (), {"Document": Document})
        if name == "llama_index.core.schema":
            return type("Nodes", (), {"TextNode": TextNode})
        if name.startswith(("sklearn", "torch")):
            raise ModuleNotFoundError(name)
        return original(name)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    document = EvidenceVectorDocument(
        document_id="a" * 64,
        evidence_id="ev-1",
        task_id="tsk-1",
        text="evidenced value",
        source_hash="b" * 64,
        field_name="magnitude",
        location="table:1",
    )

    async def exercise() -> None:
        report = await ChromaEvidenceIndex("http://chroma:8000", dimensions=64).index([document])
        assert report.indexed_count == 1
        assert report.langchain_document_count == 1
        assert report.llamaindex_node_count == 1
        empty = await ChromaEvidenceIndex("http://chroma:8000", dimensions=64).index([])
        assert empty.indexed_count == 0

    asyncio.run(exercise())
    assert upserts[0]["ids"] == ["a" * 64]
    assert upserts[0]["metadatas"] == [
        {
            "evidence_id": "ev-1",
            "task_id": "tsk-1",
            "source_hash": "b" * 64,
            "field_name": "magnitude",
            "location": "table:1",
        }
    ]
    with pytest.raises(ValueError):
        ChromaEvidenceIndex("ftp://chroma")


def test_framework_adapters_degrade_when_packages_are_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name: (_ for _ in ()).throw(ModuleNotFoundError(name)),
    )
    assert _framework_document_counts([]) == (0, 0)


def test_agent_graph_fallback_executes_fixed_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    original = importlib.import_module

    def no_langgraph(name: str) -> Any:
        if name == "langgraph.graph":
            raise ModuleNotFoundError(name)
        return original(name)

    monkeypatch.setattr(importlib, "import_module", no_langgraph)

    async def exercise() -> None:
        async def execute(submission: ResearchJobSubmission) -> ResearchJobResult:
            events.append(submission.research_goal)
            return _result()

        async def index() -> None:
            events.append("indexed")

        result = await BoundedResearchGraph(execute, index).run(_submission())
        assert result == _result()

    asyncio.run(exercise())
    assert events[-1] == "indexed"


def test_build_evidence_documents_uses_existing_values_only() -> None:
    @dataclass
    class Evidence:
        evidence_id: str = "ev-1"
        field_name: str = "flux"
        raw_value: str = "1.25"
        source_location: str = "table:2"
        method: str = "csv"
        source_hash: str = "c" * 64

    snapshot = type("Snapshot", (), {"task_id": "tsk-1", "evidence": [Evidence()]})()
    documents = build_evidence_documents(snapshot)
    assert len(documents) == 1
    assert "value=1.25" in documents[0].text
    assert documents[0].source_hash == "c" * 64


def test_platform_status_and_research_job_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    settings = Settings(_env_file=None)
    status = build_platform_status(settings)
    assert status.mode == "local"
    assert status.components[0].status == "ready"
    assert all(item.status in {"disabled", "optional", "ready"} for item in status.components)

    async def exercise() -> None:
        repository = InMemoryResearchJobRepository()

        async def execute(submission: ResearchJobSubmission) -> ResearchJobResult:
            return _result()

        jobs = ResearchJobService(repository, execute)
        provider = DemoDeliveryProvider(settings=settings)
        app = create_app(provider, research_jobs=jobs)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            platform = await client.get("/api/v1/platform")
            assert platform.status_code == 200
            submitted = await client.post(
                "/api/v1/research-jobs", json=_submission("api-key-001").model_dump(mode="json")
            )
            assert submitted.status_code == 202
            job_id = submitted.json()["job_id"]
            terminal = await _wait_terminal(jobs, job_id)
            response = await client.get(f"/api/v1/research-jobs/{terminal.job_id}")
            assert response.json()["status"] == "succeeded"
            listing = await client.get("/api/v1/research-jobs?limit=10")
            assert listing.json()["count"] == 1
            missing = await client.get("/api/v1/research-jobs/job_" + "0" * 32)
            assert missing.status_code == 400
            invalid = await client.post(
                "/api/v1/research-jobs", json={"research_goal": "too short", "extra": True}
            )
            assert invalid.status_code == 422

    asyncio.run(exercise())


def test_nested_optional_module_probe_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: (_ for _ in ()).throw(ModuleNotFoundError(name)),
    )
    assert _module_available("llama_index.core") is False
