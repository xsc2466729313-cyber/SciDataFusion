"""Bounded LangGraph orchestration for validated research jobs."""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from scidatafusion.contracts.platform import ResearchJobResult, ResearchJobSubmission

ExecuteResearch = Callable[[ResearchJobSubmission], Awaitable[ResearchJobResult]]
IndexEvidence = Callable[[], Awaitable[None]]


class _GraphState(TypedDict, total=False):
    submission: ResearchJobSubmission
    result: ResearchJobResult
    stage: str


class BoundedResearchGraph:
    """Validate, execute and index in three fixed nodes with no model-controlled edges."""

    def __init__(self, execute: ExecuteResearch, index_evidence: IndexEvidence) -> None:
        self._execute = execute
        self._index_evidence = index_evidence

    async def run(self, submission: ResearchJobSubmission) -> ResearchJobResult:
        try:
            graph_module = importlib.import_module("langgraph.graph")
        except ModuleNotFoundError:
            result = await self._execute(submission)
            await self._index_evidence()
            return result

        async def validate_node(state: _GraphState) -> _GraphState:
            validated = ResearchJobSubmission.model_validate(state["submission"])
            return {"submission": validated, "stage": "validated"}

        async def execute_node(state: _GraphState) -> _GraphState:
            result = await self._execute(state["submission"])
            return {"result": result, "stage": "executed"}

        async def index_node(state: _GraphState) -> _GraphState:
            await self._index_evidence()
            return {"stage": "indexed"}

        builder = graph_module.StateGraph(_GraphState)
        builder.add_node("validate", validate_node)
        builder.add_node("execute", execute_node)
        builder.add_node("index", index_node)
        builder.add_edge(graph_module.START, "validate")
        builder.add_edge("validate", "execute")
        builder.add_edge("execute", "index")
        builder.add_edge("index", graph_module.END)
        output: dict[str, Any] = await builder.compile().ainvoke({"submission": submission})
        return ResearchJobResult.model_validate(output["result"])
