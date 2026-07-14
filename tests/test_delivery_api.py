"""M20 FastAPI and workbench end-to-end tests."""

from __future__ import annotations

import asyncio

import httpx

from scidatafusion.api import DemoDeliveryProvider, create_app
from scidatafusion.config import Settings


async def _exercise_api() -> None:
    provider = DemoDeliveryProvider(settings=Settings(_env_file=None))
    transport = httpx.ASGITransport(app=create_app(provider))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/api/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok", "service": "scidatafusion", "module": "M22"}
        assert health.headers["x-content-type-options"] == "nosniff"

        page = await client.get("/")
        assert page.status_code == 200
        assert "SciDataFusion 科学数据融合工作台" in page.text
        assert "从科学问题到可交付数据" in page.text
        assert "联网智能" in page.text
        assert "实时联网发现" in page.text
        assert "联网配置" in page.text
        assert 'id="config-form"' in page.text
        assert 'id="cfg-base-url"' in page.text
        assert "https://dashscope.aliyuncs.com/compatible-mode/v1" in page.text
        assert "保存并应用" in page.text
        assert "规划模型" not in page.text
        assert "国家代码" not in page.text
        assert "M00-M20" not in page.text

        runtime = await client.get("/api/v1/runtime")
        assert runtime.status_code == 200
        assert runtime.json()["online_ready"] is False
        assert runtime.json()["search_endpoint_host"] == "serpapi.com"

        configuration = await client.get("/api/v1/online/configuration")
        assert configuration.status_code == 200
        assert configuration.json()["query_planning_enabled"] is True
        assert configuration.json()["max_search_queries"] == 3
        assert all(not item["configured"] for item in configuration.json()["credentials"])
        assert all(
            set(item) == {"environment_variable", "configured"}
            for item in configuration.json()["credentials"]
        )

        status = await client.get("/api/v1/demo/status")
        assert status.status_code == 200
        summary = status.json()
        assert summary["status"] == "succeeded"
        assert summary["issue_count"] == 0
        assert summary["formal_gold_record_count"] == 8
        assert summary["quality_gate_passed"] is True
        assert summary["quality_score"] == 1.0
        assert summary["known_limitations"] == []

        workbench = await client.get("/api/v1/workbench")
        assert workbench.status_code == 200
        detail = workbench.json()
        assert detail["execution_mode"] == "offline"
        assert detail["online_research"] is None
        assert [item["label"] for item in detail["stages"]] == [
            "研究需求",
            "多源发现",
            "解析提取",
            "清洗整合",
            "质量校验",
            "成果交付",
        ]
        assert len(detail["sources"]) == 3
        assert len(detail["artifacts"]) == 7
        assert len(detail["evidence"]) == 76
        assert len(detail["fields"]) == 6
        assert len(detail["chart_points"]) == 8
        assert detail["chart_points"][0] == {
            "x": "53249.29",
            "y": "15.769",
            "error_x": "0",
            "error_y": "0.006",
        }
        assert len(detail["graph_nodes"]) == 87
        assert len(detail["graph_edges"]) == 130
        assert detail["scientific_dataset"]["format"] == "fits"
        assert detail["scientific_dataset"]["variable_names"] == ["MJD", "MAG", "MAG_ERR"]
        assert detail["scientific_dataset"]["materialized_cell_count"] == 12

        issues = await client.get("/api/v1/demo/issues")
        assert issues.status_code == 200
        assert issues.json() == []

        unauthorized = await client.get("/api/v1/demo/artifacts/scidatafusion-reproduction.zip")
        assert unauthorized.status_code == 422
        ticket = await client.post("/api/v1/demo/download-tickets/scidatafusion-reproduction.zip")
        assert ticket.status_code == 200
        package = await client.get(ticket.json()["download_url"])
        assert package.status_code == 200
        assert package.headers["content-type"] == "application/zip"
        assert package.content.startswith(b"PK")

        gold_ticket = await client.post("/api/v1/demo/download-tickets/gold.csv")
        assert gold_ticket.status_code == 200
        gold = await client.get(gold_ticket.json()["download_url"])
        assert gold.status_code == 200
        assert gold.headers["content-type"].startswith("text/csv")
        lines = gold.text.splitlines()
        assert len(lines) == 9
        assert "2004dt,53249.29,VizieR:J/AJ/154/211/OptPhot:1" in lines[1]

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
