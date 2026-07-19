from __future__ import annotations

from pathlib import Path

import pytest

import scidatafusion.prompting as prompting


def test_prompt_path_uses_packaged_resource_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_root = tmp_path / "package-prompts"
    package_root.mkdir()
    packaged = package_root / "online_search_planning.md"
    packaged.write_text("packaged", encoding="utf-8")
    monkeypatch.setattr(prompting, "_PACKAGE_PROMPT_ROOT", package_root)

    assert prompting.prompt_path("online_search_planning.md") == packaged


def test_prompt_path_uses_source_resource_for_editable_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_root = tmp_path / "missing-package-prompts"
    source_root = tmp_path / "source-prompts"
    source_root.mkdir()
    source = source_root / "problem_compiler.md"
    source.write_text("source", encoding="utf-8")
    monkeypatch.setattr(prompting, "_PACKAGE_PROMPT_ROOT", package_root)
    monkeypatch.setattr(prompting, "_SOURCE_PROMPT_ROOT", source_root)

    assert prompting.prompt_path("problem_compiler.md") == source


def test_prompt_path_rejects_unknown_resource() -> None:
    with pytest.raises(ValueError, match="unknown prompt resource"):
        prompting.prompt_path("../secret.txt")
