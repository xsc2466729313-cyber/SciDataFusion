"""Resolve versioned prompt resources in source and installed distributions."""

from __future__ import annotations

from pathlib import Path

_PROMPT_NAMES = frozenset(
    {
        "online_acquisition_reflection.md",
        "online_artifact_qualification.md",
        "online_field_mapping.md",
        "online_quality_review.md",
        "online_search_planning.md",
        "online_source_assessment.md",
        "problem_compiler.md",
    }
)
_PACKAGE_PROMPT_ROOT = Path(__file__).resolve().parent / "prompts"
_SOURCE_PROMPT_ROOT = Path(__file__).resolve().parents[2] / "prompts"


def prompt_path(name: str) -> Path:
    """Return an allowlisted prompt path for both editable and wheel installs."""

    if name not in _PROMPT_NAMES:
        raise ValueError(f"unknown prompt resource: {name}")
    packaged = _PACKAGE_PROMPT_ROOT / name
    if packaged.is_file():
        return packaged
    return _SOURCE_PROMPT_ROOT / name
