"""FastAPI workbench and typed M20 delivery/download boundary."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import mimetypes
import secrets
from importlib.resources import files
from pathlib import Path as FileSystemPath
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import FastAPI, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import StringConstraints, ValidationError

from scidatafusion import __version__
from scidatafusion.cli import (
    _build_search_planning,
    _execute_offline_figure,
    _execute_offline_knowledge,
    _execute_offline_scientific,
)
from scidatafusion.config import Settings, get_settings
from scidatafusion.contracts.base import StrictContract
from scidatafusion.contracts.delivery import DeliveryArtifact, DeliveryRequest, DeliveryResult
from scidatafusion.contracts.online import (
    AutomatedQualityReview,
    OnlineConfigurationUpdate,
    OnlineConfigurationView,
    OnlineResearchResult,
    OnlineRuntimeStatus,
    ResearchExecutionMode,
)
from scidatafusion.contracts.platform import (
    PlatformStatus,
    ResearchJobPage,
    ResearchJobRecord,
    ResearchJobResult,
    ResearchJobSubmission,
)
from scidatafusion.contracts.workbench import WorkbenchSnapshot
from scidatafusion.delivery.downloads import DownloadTicketSigner
from scidatafusion.delivery.fixtures import build_offline_delivery_bundle
from scidatafusion.delivery.service import DeliveryOrchestrator
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.online import (
    AgentReflectionCoordinator,
    LocalOnlineConfigurationStore,
    OnlineAcquisitionService,
    OnlineResearchService,
    OnlineStructuredDataService,
    build_online_configuration,
    build_online_runtime_status,
)
from scidatafusion.platform.agent_graph import BoundedResearchGraph
from scidatafusion.platform.jobs import (
    CeleryJobDispatcher,
    InMemoryResearchJobRepository,
    PostgresResearchJobRepository,
    ResearchJobService,
)
from scidatafusion.platform.status import build_platform_status
from scidatafusion.platform.vectors import ChromaEvidenceIndex, build_evidence_documents
from scidatafusion.workbench import build_workbench_snapshot

DEFAULT_GOAL = "Study Type Ia supernova light curves using multi-source data integration into CSV."
REFERENCE_QUERY = "quality evidence observation time magnitude"


class DemoRunRequest(StrictContract):
    execution_mode: Literal["offline", "online"] = "offline"
    research_goal: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=10, max_length=2_000)
    ] = DEFAULT_GOAL
    retrieval_query: Annotated[
        str | None, StringConstraints(strip_whitespace=True, min_length=3, max_length=512)
    ] = None


class ArtifactSummary(StrictContract):
    filename: str
    kind: str
    media_type: str
    sha256: str
    size_bytes: int
    downloadable: bool


class DeliverySummary(StrictContract):
    status: str
    task_id: str
    run_id: str
    contract_id: str
    contract_version: str
    quality_gate_passed: bool
    formal_gold_record_count: int
    quality_score: float
    issue_count: int
    graph_node_count: int
    graph_edge_count: int
    retrieval_hit_count: int
    package_sha256: str
    package_size_bytes: int
    known_limitations: tuple[str, ...]
    artifacts: tuple[ArtifactSummary, ...]


class IssueSummary(StrictContract):
    issue_id: str
    code: str
    severity: str
    affected_fields: tuple[str, ...]
    evidence_count: int
    suggested_action: str


class DownloadTicket(StrictContract):
    filename: str
    expires_at: int
    download_url: str


class DemoDeliveryProvider:
    """Run the deterministic workflow with optional audited online discovery."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        online_service: OnlineResearchService | None = None,
        online_acquisition_service: OnlineAcquisitionService | None = None,
        online_structured_service: OnlineStructuredDataService | None = None,
        reflection_max_rounds: int = 4,
    ) -> None:
        self.settings = settings or get_settings()
        self._online_service = online_service or OnlineResearchService(self.settings)
        self._online_acquisition_service = online_acquisition_service or OnlineAcquisitionService()
        self._online_structured_service = online_structured_service or OnlineStructuredDataService()
        self._reflection_max_rounds = reflection_max_rounds
        self._reflection_coordinator = AgentReflectionCoordinator(
            self._online_service,
            self._online_acquisition_service,
            max_rounds=self._reflection_max_rounds,
        )
        self._lock = asyncio.Lock()
        self._request: DeliveryRequest | None = None
        self._result: DeliveryResult | None = None
        self._orchestrator: DeliveryOrchestrator | None = None
        self._workbench: WorkbenchSnapshot | None = None

    async def run(self, payload: DemoRunRequest) -> DeliverySummary:
        """Execute the deterministic demonstration chain and return a reduced summary."""

        async with self._lock:
            execution_mode = ResearchExecutionMode(payload.execution_mode)
            phase1, planning = await asyncio.to_thread(
                _build_search_planning,
                DEFAULT_GOAL,
                "workbench-reviewer",
            )
            if planning is None or phase1.confirmation is None:
                raise AppError(
                    ErrorCode.VALIDATION_FAILED, "research goal did not produce a contract"
                )
            knowledge_chain, figure_chain, scientific_chain = await asyncio.gather(
                _execute_offline_knowledge(
                    phase1.confirmation.contract,
                    planning,
                    REFERENCE_QUERY,
                    complete_profile=True,
                ),
                _execute_offline_figure(phase1.confirmation.contract),
                _execute_offline_scientific(phase1.confirmation.contract),
            )
            online_result: OnlineResearchResult | None = None
            online_acquisition = None
            online_structured_data = None
            online_field_mapping = None
            agent_reflection = None
            if execution_mode is ResearchExecutionMode.ONLINE:
                reflection_outcome = await self._reflection_coordinator.run(
                    research_goal=payload.research_goal,
                    query=payload.retrieval_query,
                )
                online_result = reflection_outcome.research
                online_acquisition = reflection_outcome.acquisition
                agent_reflection = reflection_outcome.trace
                online_structured_data = await asyncio.to_thread(
                    self._online_structured_service.parse,
                    online_acquisition.artifacts,
                    self._online_acquisition_service.read_artifact,
                )
                online_field_mapping = await self._online_service.map_structured_fields(
                    research_goal=payload.research_goal,
                    target_fields=online_result.search_plan.profile.candidate_fields,
                    structured_data=online_structured_data,
                )
            knowledge_request, knowledge_result, bronze_store = knowledge_chain
            _, figure_result, _ = figure_chain
            scientific_request, scientific_result, _ = scientific_chain
            bundle = build_offline_delivery_bundle(not_before=knowledge_result.created_at)
            request = DeliveryRequest(
                knowledge_request=knowledge_request,
                knowledge_result=knowledge_result,
                policy=bundle.policy,
                runtime=bundle.runtime,
                requested_at=bundle.runtime.checked_at,
            )
            orchestrator = DeliveryOrchestrator(bronze_store=bronze_store)
            result = await orchestrator.execute(request)
            automated_review: AutomatedQualityReview | None = None
            self._request = request
            self._result = result
            self._orchestrator = orchestrator
            self._workbench = build_workbench_snapshot(
                research_goal=payload.research_goal,
                retrieval_query=(
                    online_result.query
                    if online_result is not None
                    else payload.retrieval_query or payload.research_goal
                ),
                request=knowledge_request,
                knowledge=knowledge_result,
                figure=figure_result,
                scientific_request=scientific_request,
                scientific=scientific_result,
                delivery=result,
                execution_mode=execution_mode,
                online_research=online_result,
                online_acquisition=online_acquisition,
                online_structured_data=online_structured_data,
                online_field_mapping=online_field_mapping,
                agent_reflection=agent_reflection,
                automated_quality_review=automated_review,
            )
            return _summary(request, result)

    async def update_online_settings(self, settings: Settings) -> None:
        """Apply a validated local configuration without restarting the server."""

        async with self._lock:
            self.settings = settings
            self._online_service = OnlineResearchService(settings)
            self._reflection_coordinator = AgentReflectionCoordinator(
                self._online_service,
                self._online_acquisition_service,
                max_rounds=self._reflection_max_rounds,
            )

    async def current(self) -> tuple[DeliveryRequest, DeliveryResult, DeliveryOrchestrator]:
        """Return the current delivery, creating the default demonstration when absent."""

        if self._result is None:
            await self.run(DemoRunRequest())
        if self._request is None or self._result is None or self._orchestrator is None:
            raise AppError(ErrorCode.INTERNAL_ERROR, "M20 demo state is unavailable")
        return self._request, self._result, self._orchestrator

    async def workbench(self) -> WorkbenchSnapshot:
        """Return the current complete product projection."""

        if self._workbench is None:
            await self.run(DemoRunRequest())
        if self._workbench is None:
            raise AppError(ErrorCode.INTERNAL_ERROR, "workbench state is unavailable")
        return self._workbench

    async def read_online_artifact(self, byte_sha256: str) -> tuple[bytes, str]:
        """Read a current-topic Bronze artifact only after hash and scope verification."""

        snapshot = await self.workbench()
        artifact = next(
            (item for item in snapshot.artifacts if item.sha256 == byte_sha256),
            None,
        )
        if snapshot.topic_data_status != "live_discovery" or artifact is None:
            raise AppError(ErrorCode.INVALID_REQUEST, "Unknown current-topic online artifact")
        return self._online_acquisition_service.read_artifact(byte_sha256), artifact.media_type

    async def read_online_evidence_table(self) -> bytes:
        """Build the current task's provenance-rich long table from verified Bronze bytes."""

        snapshot = await self.workbench()
        if (
            snapshot.topic_data_status != "live_discovery"
            or snapshot.online_acquisition is None
            or snapshot.online_field_mapping is None
            or not snapshot.online_field_mapping.decisions
        ):
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "当前任务没有可导出的结构化证据表。",
            )
        return await asyncio.to_thread(
            self._online_structured_service.build_evidence_csv,
            snapshot.online_acquisition.artifacts,
            self._online_acquisition_service.read_artifact,
            snapshot.online_field_mapping,
        )


def create_app(
    provider: DemoDeliveryProvider | None = None,
    configuration_store: LocalOnlineConfigurationStore | None = None,
    research_jobs: ResearchJobService | None = None,
) -> FastAPI:
    """Create the SciDataFusion workbench application with injectable demo state."""

    app = FastAPI(
        title="SciDataFusion Workbench",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
    )
    state = provider or DemoDeliveryProvider()
    local_configuration = configuration_store or LocalOnlineConfigurationStore(
        FileSystemPath(state.settings.local_configuration_file)
    )
    ticket_signer = DownloadTicketSigner(secrets.token_bytes(32))
    jobs = research_jobs or _build_research_job_service(state)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        status_code = 409 if exc.code is ErrorCode.QUALITY_GATE_FAILED else 400
        if exc.code is ErrorCode.SECURITY_POLICY_VIOLATION:
            status_code = 403
        if exc.code is ErrorCode.INTERNAL_ERROR:
            status_code = 500
        return JSONResponse(
            exc.to_problem_details(instance=str(request.url.path)),
            status_code=status_code,
            media_type="application/problem+json",
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            {
                "type": "urn:scidatafusion:error:invalid_request",
                "title": "Invalid Request",
                "detail": "Request validation failed.",
                "code": "invalid_request",
                "instance": str(request.url.path),
                "errors": [
                    {"type": item["type"], "loc": item["loc"], "msg": item["msg"]}
                    for item in exc.errors()
                ],
            },
            status_code=422,
            media_type="application/problem+json",
        )

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def workbench() -> HTMLResponse:
        react_index = files("scidatafusion.web").joinpath("react").joinpath("index.html")
        page = (
            react_index.read_text(encoding="utf-8")
            if react_index.is_file()
            else files("scidatafusion.web").joinpath("index.html").read_text(encoding="utf-8")
        )
        return HTMLResponse(page)

    @app.get("/app-assets/{filename:path}", include_in_schema=False)
    async def react_asset(
        filename: Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")],
    ) -> Response:
        if ".." in filename.split("/"):
            raise AppError(ErrorCode.INVALID_REQUEST, "Invalid application asset path")
        asset = files("scidatafusion.web").joinpath("react").joinpath("app-assets")
        for segment in filename.split("/"):
            asset = asset.joinpath(segment)
        if not asset.is_file():
            raise AppError(ErrorCode.INVALID_REQUEST, "Unknown application asset")
        media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return Response(asset.read_bytes(), media_type=media_type)

    @app.get("/assets/knowledge-graph.js", include_in_schema=False)
    async def knowledge_graph_asset() -> Response:
        script = files("scidatafusion.web").joinpath("knowledge-graph.js").read_bytes()
        return Response(script, media_type="text/javascript")

    @app.get("/assets/three.module.min.js", include_in_schema=False)
    async def three_asset() -> Response:
        script = (
            files("scidatafusion.web")
            .joinpath("vendor")
            .joinpath("three.module.min.js")
            .read_bytes()
        )
        return Response(script, media_type="text/javascript")

    @app.get("/assets/three.core.min.js", include_in_schema=False)
    async def three_core_asset() -> Response:
        script = (
            files("scidatafusion.web").joinpath("vendor").joinpath("three.core.min.js").read_bytes()
        )
        return Response(script, media_type="text/javascript")

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "scidatafusion", "module": "M28"}

    @app.get("/api/v1/platform", response_model=PlatformStatus)
    async def platform_status() -> PlatformStatus:
        return build_platform_status(state.settings)

    @app.post(
        "/api/v1/research-jobs",
        response_model=ResearchJobRecord,
        status_code=202,
    )
    async def submit_research_job(payload: ResearchJobSubmission) -> ResearchJobRecord:
        return await jobs.submit(payload)

    @app.get("/api/v1/research-jobs", response_model=ResearchJobPage)
    async def list_research_jobs(
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> ResearchJobPage:
        return await jobs.list(limit)

    @app.get("/api/v1/research-jobs/{job_id}", response_model=ResearchJobRecord)
    async def get_research_job(
        job_id: Annotated[str, Path(pattern=r"^job_[0-9a-f]{32}$")],
    ) -> ResearchJobRecord:
        record = await jobs.get(job_id)
        if record is None:
            raise AppError(ErrorCode.INVALID_REQUEST, "Unknown research job")
        return record

    @app.get("/api/v1/runtime", response_model=OnlineRuntimeStatus)
    async def runtime_status() -> OnlineRuntimeStatus:
        return build_online_runtime_status(state.settings)

    @app.get("/api/v1/online/configuration", response_model=OnlineConfigurationView)
    async def online_configuration() -> OnlineConfigurationView:
        return build_online_configuration(state.settings)

    @app.put("/api/v1/online/configuration", response_model=OnlineConfigurationView)
    async def update_online_configuration(
        payload: OnlineConfigurationUpdate,
        request: Request,
    ) -> OnlineConfigurationView:
        host = None if request.client is None else request.client.host
        try:
            is_loopback = host is not None and ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            raise AppError(
                ErrorCode.SECURITY_POLICY_VIOLATION,
                "online configuration may be changed only from the local machine",
            )
        try:
            settings = await asyncio.to_thread(local_configuration.save, payload)
        except ValidationError as exc:
            raise AppError(
                ErrorCode.CONFIGURATION_ERROR,
                "online configuration failed validation",
            ) from exc
        await state.update_online_settings(settings)
        return build_online_configuration(settings)

    @app.post("/api/v1/demo/run", response_model=DeliverySummary)
    async def run_demo(payload: DemoRunRequest) -> DeliverySummary:
        return await state.run(payload)

    @app.get("/api/v1/demo/status", response_model=DeliverySummary)
    async def demo_status() -> DeliverySummary:
        request, result, _ = await state.current()
        return _summary(request, result)

    @app.get("/api/v1/workbench", response_model=WorkbenchSnapshot)
    async def workbench_snapshot() -> WorkbenchSnapshot:
        return await state.workbench()

    @app.get("/api/v1/demo/issues", response_model=tuple[IssueSummary, ...])
    async def demo_issues() -> tuple[IssueSummary, ...]:
        request, _, _ = await state.current()
        return tuple(
            IssueSummary(
                issue_id=item.issue_id,
                code=item.code.value,
                severity=item.severity.value,
                affected_fields=item.affected_field_names,
                evidence_count=len(item.evidence_refs),
                suggested_action=item.suggested_action.value,
            )
            for item in request.knowledge_request.quality_result.issue_set.issues
        )

    @app.post(
        "/api/v1/demo/download-tickets/{filename}",
        response_model=DownloadTicket,
    )
    async def issue_download_ticket(
        filename: Annotated[
            str,
            Path(pattern=r"^[a-z0-9][a-z0-9_.-]{0,127}$"),
        ],
    ) -> DownloadTicket:
        _, result, _ = await state.current()
        available = {item.filename: item for item in (*result.manifest.files, result.package)}
        artifact = available.get(filename)
        if artifact is None:
            if filename in {"gold.csv", "gold.parquet"}:
                raise AppError(
                    ErrorCode.QUALITY_GATE_FAILED,
                    "Formal Gold export is blocked until all required quality gates pass.",
                    details={"available_review_package": result.package.filename},
                )
            raise AppError(ErrorCode.INVALID_REQUEST, "Unknown M20 delivery artifact")
        token, expires_at = ticket_signer.issue(artifact)
        encoded_filename = quote(artifact.filename, safe="")
        return DownloadTicket(
            filename=artifact.filename,
            expires_at=expires_at,
            download_url=(
                f"/api/v1/demo/artifacts/{encoded_filename}"
                f"?token={quote(token, safe='')}&expires_at={expires_at}"
            ),
        )

    @app.get("/api/v1/demo/artifacts/{filename}")
    async def download_artifact(
        filename: Annotated[
            str,
            Path(pattern=r"^[a-z0-9][a-z0-9_.-]{0,127}$"),
        ],
        token: Annotated[str, Query(min_length=43, max_length=128)],
        expires_at: Annotated[int, Query(gt=0)],
    ) -> Response:
        _, result, orchestrator = await state.current()
        available = {item.filename: item for item in (*result.manifest.files, result.package)}
        artifact = available.get(filename)
        if artifact is None:
            raise AppError(ErrorCode.INVALID_REQUEST, "Unknown M20 delivery artifact")
        ticket_signer.verify(artifact, token, expires_at)
        payload = orchestrator.delivery_store.get(artifact.sha256)
        if payload is None:
            raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, "M20 artifact bytes are missing")
        return Response(
            payload,
            media_type=artifact.media_type,
            headers={"Content-Disposition": f'attachment; filename="{artifact.filename}"'},
        )

    @app.get("/api/v1/online/artifacts/{byte_sha256}")
    async def download_online_artifact(
        byte_sha256: Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")],
    ) -> Response:
        payload, media_type = await state.read_online_artifact(byte_sha256)
        suffix = _online_artifact_suffix(media_type)
        return Response(
            payload,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{byte_sha256}{suffix}"',
                "Content-Length": str(len(payload)),
                "X-Content-SHA256": byte_sha256,
            },
        )

    @app.get("/api/v1/online/evidence-table.csv")
    async def download_online_evidence_table() -> Response:
        payload = await state.read_online_evidence_table()
        digest = hashlib.sha256(payload).hexdigest()
        return Response(
            payload,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    'attachment; filename="scidatafusion-current-topic-evidence.csv"'
                ),
                "Content-Length": str(len(payload)),
                "X-Content-SHA256": digest,
            },
        )

    return app


def _build_research_job_service(state: DemoDeliveryProvider) -> ResearchJobService:
    settings = state.settings
    if settings.platform_mode.value == "celery":
        if settings.database_url is None or settings.redis_url is None:
            raise AppError(ErrorCode.CONFIGURATION_ERROR, "platform persistence is not configured")
        persistent_repository = PostgresResearchJobRepository(
            settings.database_url.get_secret_value(),
            timeout_seconds=settings.platform_connection_timeout_seconds,
        )
        dispatcher = CeleryJobDispatcher(settings.redis_url.get_secret_value())

        async def unused_executor(submission: ResearchJobSubmission) -> ResearchJobResult:
            raise RuntimeError("Celery workers execute platform jobs")

        return ResearchJobService(persistent_repository, unused_executor, dispatcher=dispatcher)

    local_repository = InMemoryResearchJobRepository()

    async def execute(submission: ResearchJobSubmission) -> ResearchJobResult:
        return await execute_research_submission(state, submission)

    return ResearchJobService(local_repository, execute)


async def execute_research_submission(
    state: DemoDeliveryProvider, submission: ResearchJobSubmission
) -> ResearchJobResult:
    """Run one validated job through the bounded graph and optional evidence index."""

    async def run_workflow(payload: ResearchJobSubmission) -> ResearchJobResult:
        summary = await state.run(
            DemoRunRequest(
                execution_mode=payload.execution_mode,
                research_goal=payload.research_goal,
                retrieval_query=payload.retrieval_query,
            )
        )
        snapshot = await state.workbench()
        return ResearchJobResult(
            task_id=summary.task_id,
            run_id=summary.run_id,
            quality_gate_passed=summary.quality_gate_passed,
            quality_score=summary.quality_score,
            source_count=len(snapshot.sources),
            evidence_count=len(snapshot.evidence),
            artifact_count=len(snapshot.artifacts),
            issue_count=summary.issue_count,
            formal_gold_record_count=summary.formal_gold_record_count,
            package_filename=snapshot.package_filename,
        )

    async def index_evidence() -> None:
        settings = state.settings
        if settings.chroma_url is None:
            return
        snapshot = await state.workbench()
        index = ChromaEvidenceIndex(str(settings.chroma_url), dimensions=settings.vector_dimensions)
        await index.index(build_evidence_documents(snapshot))

    graph = BoundedResearchGraph(run_workflow, index_evidence)
    return await graph.run(submission)


def _online_artifact_suffix(media_type: str) -> str:
    return {
        "application/fits": ".fits",
        "application/geo+json": ".geojson",
        "application/json": ".json",
        "application/pdf": ".pdf",
        "application/vnd.apache.parquet": ".parquet",
        "application/zip": ".zip",
        "text/csv": ".csv",
        "text/html": ".html",
        "text/plain": ".txt",
        "text/tab-separated-values": ".tsv",
    }.get(media_type.casefold(), ".bin")


def _summary(request: DeliveryRequest, result: DeliveryResult) -> DeliverySummary:
    quality = request.knowledge_request.quality_result
    knowledge = request.knowledge_result
    artifacts = (*result.manifest.files, result.package)
    return DeliverySummary(
        status=result.status.value,
        task_id=result.task_id,
        run_id=result.run_id,
        contract_id=result.contract_id,
        contract_version=result.contract_version,
        quality_gate_passed=quality.quality_report.quality_gate_passed,
        formal_gold_record_count=result.metrics.formal_gold_record_count,
        quality_score=quality.quality_report.quality_score,
        issue_count=quality.metrics.issue_count,
        graph_node_count=knowledge.metrics.graph_node_count,
        graph_edge_count=knowledge.metrics.graph_edge_count,
        retrieval_hit_count=knowledge.metrics.retrieval_hit_count,
        package_sha256=result.package.sha256,
        package_size_bytes=result.package.size_bytes,
        known_limitations=result.manifest.known_limitations,
        artifacts=tuple(_artifact_summary(item) for item in artifacts),
    )


def _artifact_summary(item: DeliveryArtifact) -> ArtifactSummary:
    return ArtifactSummary(
        filename=item.filename,
        kind=item.kind.value,
        media_type=item.media_type,
        sha256=item.sha256,
        size_bytes=item.size_bytes,
        downloadable=True,
    )


app = create_app()
