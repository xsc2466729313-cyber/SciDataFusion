"""M20 FastAPI and workbench end-to-end tests."""

from __future__ import annotations

import asyncio

import httpx

from scidatafusion.api import create_app


async def _exercise_api() -> None:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/api/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok", "service": "scidatafusion", "module": "M20"}
        assert health.headers["x-content-type-options"] == "nosniff"

        page = await client.get("/")
        assert page.status_code == 200
        assert "SciDataFusion Workbench" in page.text
        assert "运行 M00-M20" in page.text

        status = await client.get("/api/v1/demo/status")
        assert status.status_code == 200
        summary = status.json()
        assert summary["status"] == "needs_review"
        assert summary["issue_count"] == 3
        assert summary["formal_gold_record_count"] == 0
        assert summary["known_limitations"]

        issues = await client.get("/api/v1/demo/issues")
        assert issues.status_code == 200
        assert len(issues.json()) == 3
        assert all(item["evidence_count"] > 0 for item in issues.json())

        unauthorized = await client.get("/api/v1/demo/artifacts/scidatafusion-reproduction.zip")
        assert unauthorized.status_code == 422
        ticket = await client.post("/api/v1/demo/download-tickets/scidatafusion-reproduction.zip")
        assert ticket.status_code == 200
        package = await client.get(ticket.json()["download_url"])
        assert package.status_code == 200
        assert package.headers["content-type"] == "application/zip"
        assert package.content.startswith(b"PK")

        blocked = await client.post("/api/v1/demo/download-tickets/gold.csv")
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "quality_gate_failed"
        assert blocked.headers["content-type"].startswith("application/problem+json")

        tampered_url = ticket.json()["download_url"].replace("token=", "token=x")
        tampered = await client.get(tampered_url)
        assert tampered.status_code == 403
        assert tampered.json()["code"] == "security_policy_violation"

        invalid = await client.post(
            "/api/v1/demo/run",
            json={
                "research_goal": "short",
                "retrieval_query": "quality",
                "unexpected": True,
            },
        )
        assert invalid.status_code == 422
        assert invalid.json()["code"] == "invalid_request"
        assert '"research_goal":"short"' not in invalid.text


def test_fastapi_workbench_and_download_loop() -> None:
    asyncio.run(_exercise_api())
