"""FastAPI workbench and typed M20 delivery/download boundary."""

from __future__ import annotations

import asyncio
import ipaddress
import secrets
from importlib.resources import files
from pathlib import Path as FileSystemPath
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import FastAPI, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import StringConstraints, ValidationError

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
    OnlineConfigurationUpdate,
    OnlineConfigurationView,
    OnlineResearchResult,
    OnlineRuntimeStatus,
    ResearchExecutionMode,
)
from scidatafusion.contracts.workbench import WorkbenchSnapshot
from scidatafusion.delivery.downloads import DownloadTicketSigner
from scidatafusion.delivery.fixtures import build_offline_delivery_bundle
from scidatafusion.delivery.service import DeliveryOrchestrator
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.online import (
    LocalOnlineConfigurationStore,
    OnlineResearchService,
    build_online_configuration,
    build_online_runtime_status,
)
from scidatafusion.workbench import build_workbench_snapshot

DEFAULT_GOAL = "Study Type Ia supernova light curves using multi-source data integration into CSV."
DEFAULT_QUERY = "quality evidence observation time magnitude"


class DemoRunRequest(StrictContract):
    execution_mode: Literal["offline", "online"] = "offline"
    research_goal: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=10, max_length=2_000)
    ] = DEFAULT_GOAL
    retrieval_query: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=3, max_length=512)
    ] = DEFAULT_QUERY


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
    ) -> None:
        self.settings = settings or get_settings()
        self._online_service = online_service or OnlineResearchService(self.settings)
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
                payload.research_goal,
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
                    payload.retrieval_query,
                ),
                _execute_offline_figure(phase1.confirmation.contract),
                _execute_offline_scientific(phase1.confirmation.contract),
            )
            online_result: OnlineResearchResult | None = None
            if execution_mode is ResearchExecutionMode.ONLINE:
                online_result = await self._online_service.run(
                    research_goal=payload.research_goal,
                    query=payload.retrieval_query,
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
            self._request = request
            self._result = result
            self._orchestrator = orchestrator
            self._workbench = build_workbench_snapshot(
                research_goal=payload.research_goal,
                retrieval_query=payload.retrieval_query,
                request=knowledge_request,
                knowledge=knowledge_result,
                figure=figure_result,
                scientific_request=scientific_request,
                scientific=scientific_result,
                delivery=result,
                execution_mode=execution_mode,
                online_research=online_result,
            )
            return _summary(request, result)

    async def update_online_settings(self, settings: Settings) -> None:
        """Apply a validated local configuration without restarting the server."""

        async with self._lock:
            self.settings = settings
            self._online_service = OnlineResearchService(settings)

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


def create_app(
    provider: DemoDeliveryProvider | None = None,
    configuration_store: LocalOnlineConfigurationStore | None = None,
) -> FastAPI:
    """Create the SciDataFusion workbench application with injectable demo state."""

    app = FastAPI(
        title="SciDataFusion Workbench",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url=None,
    )
    state = provider or DemoDeliveryProvider()
    local_configuration = configuration_store or LocalOnlineConfigurationStore(
        FileSystemPath(".env")
    )
    ticket_signer = DownloadTicketSigner(secrets.token_bytes(32))

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
        page = files("scidatafusion.web").joinpath("index.html").read_text(encoding="utf-8")
        return HTMLResponse(page)

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "scidatafusion", "module": "M22"}

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

    return app


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
