"""Checkpointed execute-evaluate-reflect loop for online source acquisition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import urlsplit

from pydantic import ValidationError

from scidatafusion.contracts.model import ModelInvocationRecord
from scidatafusion.contracts.online import (
    AgentReflectionProposal,
    AgentReflectionRound,
    AgentReflectionTrace,
    ArtifactQualification,
    ArtifactReviewInput,
    OnlineAcquiredArtifact,
    OnlineAcquisitionFailure,
    OnlineAcquisitionResult,
    OnlineResearchResult,
    ReflectionGapCode,
)
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.errors import AppError
from scidatafusion.online.repository import DuckDBOnlineArtifactRepository

_TARGET_ARTIFACTS = 3
_TARGET_USEFUL_ARTIFACTS = 1
_TARGET_SOURCE_DOMAINS = 2
_MACHINE_READABLE_KINDS = frozenset({"table", "scientific_file", "archive"})


class ResearchAgent(Protocol):
    async def run(
        self, *, research_goal: str, query: str | None = None
    ) -> OnlineResearchResult: ...

    async def propose_acquisition_reflection(
        self,
        *,
        research_goal: str,
        previous_queries: tuple[str, ...],
        gaps: tuple[str, ...],
        acquisition: OnlineAcquisitionResult,
    ) -> tuple[AgentReflectionProposal, ModelInvocationRecord]: ...

    async def qualify_acquired_artifacts(
        self,
        *,
        research_goal: str,
        artifacts: tuple[ArtifactReviewInput, ...],
    ) -> tuple[tuple[ArtifactQualification, ...], ModelInvocationRecord]: ...


class AcquisitionAgent(Protocol):
    async def acquire(self, research: OnlineResearchResult) -> OnlineAcquisitionResult: ...

    def build_review_inputs(
        self, artifacts: tuple[OnlineAcquiredArtifact, ...]
    ) -> tuple[ArtifactReviewInput, ...]: ...


@dataclass(frozen=True, slots=True)
class AgentReflectionOutcome:
    research: OnlineResearchResult
    acquisition: OnlineAcquisitionResult
    trace: AgentReflectionTrace


class AgentReflectionCoordinator:
    """Repeat retrieval with strict reflection until the evidence-material target is met."""

    def __init__(
        self,
        research_agent: ResearchAgent,
        acquisition_agent: AcquisitionAgent,
        *,
        repository: DuckDBOnlineArtifactRepository | None = None,
        max_rounds: int = 4,
    ) -> None:
        if not 1 <= max_rounds <= 4:
            raise ValueError("reflection rounds must be between one and four")
        self._research = research_agent
        self._acquisition = acquisition_agent
        self._repository = repository or DuckDBOnlineArtifactRepository()
        self._max_rounds = max_rounds

    async def run(self, *, research_goal: str, query: str | None) -> AgentReflectionOutcome:
        next_query = query
        previous_queries: list[str] = []
        all_artifacts: list[OnlineAcquiredArtifact] = []
        all_failures: list[OnlineAcquisitionFailure] = []
        all_hosts: list[str] = []
        policy_hashes: list[str] = []
        rounds: list[AgentReflectionRound] = []
        qualifications: dict[str, ArtifactQualification] = {}
        latest_research: OnlineResearchResult | None = None
        latest_acquisition: OnlineAcquisitionResult | None = None

        for iteration in range(1, self._max_rounds + 1):
            latest_research = await self._research.run(
                research_goal=research_goal,
                query=next_query,
            )
            previous_queries.append(latest_research.query)
            latest_acquisition = await self._acquisition.acquire(latest_research)
            all_artifacts.extend(latest_acquisition.artifacts)
            all_failures.extend(latest_acquisition.failures)
            policy_hashes.append(latest_acquisition.policy_hash)
            for host in latest_acquisition.allowed_hosts:
                if host not in all_hosts:
                    all_hosts.append(host)

            unique_artifacts = {item.byte_sha256: item for item in all_artifacts}
            review_candidates = tuple(
                item
                for key, item in unique_artifacts.items()
                if _is_machine_readable_artifact(item) and key not in qualifications
            )
            round_qualifications: tuple[ArtifactQualification, ...] = ()
            qualification_invocation: ModelInvocationRecord | None = None
            if review_candidates:
                try:
                    review_inputs = self._acquisition.build_review_inputs(review_candidates)
                    (
                        round_qualifications,
                        qualification_invocation,
                    ) = await self._research.qualify_acquired_artifacts(
                        research_goal=research_goal,
                        artifacts=review_inputs,
                    )
                except (AppError, ValidationError, ValueError):
                    round_qualifications = tuple(
                        ArtifactQualification(
                            byte_sha256=item.byte_sha256,
                            relevant_to_goal=False,
                            contains_scientific_records=False,
                            confidence=0.0,
                            accepted=False,
                            rationale="语义内容评审失败; 按不满足材料目标处理。",
                        )
                        for item in review_candidates
                    )
                qualifications.update((item.byte_sha256, item) for item in round_qualifications)
            useful_artifacts = {
                key: item
                for key, item in unique_artifacts.items()
                if qualifications.get(key) is not None and qualifications[key].accepted
            }
            domains = {
                urlsplit(str(item.source_url)).hostname
                for item in unique_artifacts.values()
                if urlsplit(str(item.source_url)).hostname is not None
            }
            gaps = self._gaps(
                artifact_count=len(unique_artifacts),
                useful_count=len(useful_artifacts),
                domain_count=len(domains),
                current=latest_acquisition,
            )
            target_met = not gaps
            proposal: AgentReflectionProposal | None = None
            invocation: ModelInvocationRecord | None = None
            strategy: Literal["initial", "llm", "fallback"] = "initial"
            decision: Literal["continue", "target_met", "checkpointed"]
            if target_met:
                decision = "target_met"
                next_query = None
                summary = "已取得足量、跨来源且经内容评审确认包含科学记录的机器可读材料。"
            else:
                decision = "checkpointed" if iteration == self._max_rounds else "continue"
                try:
                    proposal, invocation = await self._research.propose_acquisition_reflection(
                        research_goal=research_goal,
                        previous_queries=tuple(previous_queries),
                        gaps=tuple(gaps),
                        acquisition=latest_acquisition,
                    )
                    next_query = proposal.next_query
                    summary = proposal.summary
                    strategy = "llm"
                except (AppError, ValidationError, ValueError):
                    next_query = self._fallback_query(research_goal, tuple(previous_queries), gaps)
                    summary = "反思输出未通过严格校验; 已使用确定性差距查询继续。"
                    strategy = "fallback"

            proof_payload = {
                "iteration": iteration,
                "input_query": latest_research.query,
                "discovered_source_count": len(latest_research.sources),
                "attempted_download_count": latest_acquisition.attempted_count,
                "acquired_artifact_count": len(latest_acquisition.artifacts),
                "useful_artifact_count": sum(item.accepted for item in round_qualifications),
                "source_domain_count": len(domains),
                "failure_count": len(latest_acquisition.failures),
                "gaps": gaps,
                "decision": decision,
                "reflection_strategy": strategy,
                "reflection_summary": summary,
                "next_query": next_query,
                "model_invocation": None
                if invocation is None
                else invocation.model_dump(mode="json"),
                "qualifications": [item.model_dump(mode="json") for item in round_qualifications],
                "qualification_model_invocation": (
                    None
                    if qualification_invocation is None
                    else qualification_invocation.model_dump(mode="json")
                ),
            }
            reflection_round = AgentReflectionRound(
                iteration=iteration,
                input_query=latest_research.query,
                discovered_source_count=len(latest_research.sources),
                attempted_download_count=latest_acquisition.attempted_count,
                acquired_artifact_count=len(latest_acquisition.artifacts),
                useful_artifact_count=sum(item.accepted for item in round_qualifications),
                source_domain_count=len(domains),
                failure_count=len(latest_acquisition.failures),
                gaps=gaps,
                decision=decision,
                reflection_strategy=strategy,
                reflection_summary=summary,
                next_query=next_query,
                model_invocation=invocation,
                qualifications=round_qualifications,
                qualification_model_invocation=qualification_invocation,
                proof_hash=canonical_hash(proof_payload),
            )
            rounds.append(reflection_round)
            self._repository.persist_reflection_round(reflection_round)
            if target_met:
                break

        if latest_research is None or latest_acquisition is None:
            raise RuntimeError("reflection coordinator completed without a research round")
        unique_final = {item.byte_sha256: item for item in all_artifacts}
        useful_final = {
            key: item
            for key, item in unique_final.items()
            if qualifications.get(key) is not None and qualifications[key].accepted
        }
        domain_final = {
            urlsplit(str(item.source_url)).hostname
            for item in unique_final.values()
            if urlsplit(str(item.source_url)).hostname is not None
        }
        combined = OnlineAcquisitionResult(
            attempted_count=len(all_artifacts) + len(all_failures),
            artifacts=tuple(all_artifacts),
            failures=tuple(all_failures),
            allowed_hosts=tuple(all_hosts),
            policy_hash=canonical_hash({"round_policy_hashes": policy_hashes}),
            catalog=latest_acquisition.catalog,
        )
        trace = AgentReflectionTrace(
            status="target_met" if rounds[-1].decision == "target_met" else "checkpointed",
            target_artifact_count=_TARGET_ARTIFACTS,
            target_useful_artifact_count=_TARGET_USEFUL_ARTIFACTS,
            target_source_domain_count=_TARGET_SOURCE_DOMAINS,
            rounds=tuple(rounds),
            unique_artifact_count=len(unique_final),
            useful_artifact_count=len(useful_final),
            source_domain_count=len(domain_final),
        )
        return AgentReflectionOutcome(
            research=latest_research,
            acquisition=combined,
            trace=trace,
        )

    @staticmethod
    def _gaps(
        *,
        artifact_count: int,
        useful_count: int,
        domain_count: int,
        current: OnlineAcquisitionResult,
    ) -> tuple[ReflectionGapCode, ...]:
        gaps: list[ReflectionGapCode] = []
        if artifact_count == 0:
            gaps.append("no_artifact")
        elif artifact_count < _TARGET_ARTIFACTS:
            gaps.append("insufficient_artifacts")
        if useful_count < _TARGET_USEFUL_ARTIFACTS:
            gaps.append("no_useful_file")
        if domain_count < _TARGET_SOURCE_DOMAINS:
            gaps.append("insufficient_source_diversity")
        if any(item.retryable for item in current.failures):
            gaps.append("retryable_failure")
        return tuple(gaps)

    @staticmethod
    def _fallback_query(
        research_goal: str,
        previous_queries: tuple[str, ...],
        gaps: tuple[ReflectionGapCode, ...],
    ) -> str:
        route = (
            "direct public downloadable CSV GeoJSON Parquet scientific dataset"
            if "no_useful_file" in gaps
            else "open repository supplementary data archive public download"
        )
        candidate = f"{research_goal[:360]} {route}".strip()
        normalized_previous = {" ".join(item.casefold().split()) for item in previous_queries}
        if " ".join(candidate.casefold().split()) in normalized_previous:
            candidate = f"{research_goal[:330]} institutional research data catalog tabular records"
        return candidate[:512]


def _is_machine_readable_artifact(artifact: OnlineAcquiredArtifact) -> bool:
    """Return whether bytes can feed a deterministic data parser after format validation."""

    return artifact.artifact_kind in _MACHINE_READABLE_KINDS
