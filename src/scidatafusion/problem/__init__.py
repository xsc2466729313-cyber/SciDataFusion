"""Public API for the M01 scientific problem compiler."""

from scidatafusion.problem.compiler import (
    AmbiguityDetector,
    AssumptionRegistry,
    CandidateExtractor,
    ProblemCompilerAgent,
    ProblemCompilerInputError,
    ProblemSpecValidator,
)
from scidatafusion.problem.fallback import DeterministicCandidateExtractor
from scidatafusion.problem.qwen import QwenCandidateExtractor

__all__ = [
    "AmbiguityDetector",
    "AssumptionRegistry",
    "CandidateExtractor",
    "DeterministicCandidateExtractor",
    "ProblemCompilerAgent",
    "ProblemCompilerInputError",
    "ProblemSpecValidator",
    "QwenCandidateExtractor",
]
