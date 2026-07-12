from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_default_registry_loaders_work_from_an_isolated_package_copy(tmp_path: Path) -> None:
    """Exercise package-relative defaults without relying on the repository root."""

    installed_root = tmp_path / "installed"
    package_root = installed_root / "scidatafusion"
    shutil.copytree(Path("src/scidatafusion"), package_root)
    workdir = tmp_path / "outside-repository"
    workdir.mkdir()
    script = f"""
import sys
from pathlib import Path

installed_root = Path({str(installed_root)!r})
sys.path.insert(0, str(installed_root))

import scidatafusion
from scidatafusion.domain.registry import DomainPackRegistry, TaskPackRegistry
from scidatafusion.parsing.registry import ParserCapabilityRegistryLoader
from scidatafusion.schema.registry import SchemaPackRegistry
from scidatafusion.search.registry import SourceCapabilityRegistryLoader

assert Path(scidatafusion.__file__).resolve().is_relative_to(installed_root.resolve())
domain = DomainPackRegistry.load_default()
task = TaskPackRegistry.load_default()
schema = SchemaPackRegistry.load_default()
search = SourceCapabilityRegistryLoader.load_default()
parsers = ParserCapabilityRegistryLoader.load_default()
vizier = next(item for item in search.capabilities if item.source_id == "vizier_tap")
assert domain.packs
assert task.packs
assert schema.packs
assert any(item.parser_id == "m09.pdf_text" for item in parsers.parsers)
assert vizier.operations[0].supports_pagination is False
"""

    child_environment = os.environ.copy()
    for name in tuple(child_environment):
        if name.startswith(("COV_CORE_", "COVERAGE_")):
            child_environment.pop(name)

    completed = subprocess.run(  # noqa: S603 - executable and script are test-controlled
        [sys.executable, "-I", "-c", script],
        cwd=workdir,
        env=child_environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
