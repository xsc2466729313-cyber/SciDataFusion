"""Celery worker entrypoint for persisted SciDataFusion research jobs."""

from __future__ import annotations

import asyncio

from celery import Celery  # type: ignore[import-not-found]

from scidatafusion.api import DemoDeliveryProvider, execute_research_submission
from scidatafusion.config import Settings
from scidatafusion.contracts.platform import ResearchJobResult, ResearchJobSubmission
from scidatafusion.platform.jobs import PostgresResearchJobRepository, ResearchJobService

settings = Settings()
if settings.redis_url is None or settings.database_url is None:
    raise RuntimeError("Celery mode requires SCIDATA_REDIS_URL and SCIDATA_DATABASE_URL")

redis_url = settings.redis_url.get_secret_value()
database_url = settings.database_url.get_secret_value()
app = Celery("scidatafusion", broker=redis_url, backend=redis_url)
app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)


async def _execute(job_id: str, raw_submission: dict[str, object]) -> None:
    submission = ResearchJobSubmission.model_validate(raw_submission)
    runtime_settings = Settings(_env_file=settings.local_configuration_file)
    repository = PostgresResearchJobRepository(
        database_url,
        timeout_seconds=runtime_settings.platform_connection_timeout_seconds,
    )
    record = await repository.get(job_id)
    if record is None or record.submission != submission:
        raise ValueError("Celery job payload does not match the persisted submission")
    provider = DemoDeliveryProvider(settings=runtime_settings)

    async def run(payload: ResearchJobSubmission) -> ResearchJobResult:
        return await execute_research_submission(provider, payload)

    service = ResearchJobService(repository, run)
    await service.execute(job_id)


@app.task(name="scidatafusion.execute_research_job")  # type: ignore[untyped-decorator]
def execute_research_job(job_id: str, raw_submission: dict[str, object]) -> None:
    asyncio.run(_execute(job_id, raw_submission))
