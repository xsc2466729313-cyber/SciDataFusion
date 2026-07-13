"""Deterministic ZIP and notebook builders for M20."""

from __future__ import annotations

import io
import json
import zipfile

from scidatafusion.delivery.exporters import canonical_json_bytes
from scidatafusion.errors import AppError, ErrorCode

_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class NotebookGenerator:
    """Build a dependency-free notebook that verifies package entry hashes."""

    def build(self) -> bytes:
        source = (
            "import hashlib, json, pathlib\n"
            "root = pathlib.Path('.')\n"
            "manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))\n"
            "for item in manifest['files']:\n"
            "    payload = (root / item['filename']).read_bytes()\n"
            "    assert hashlib.sha256(payload).hexdigest() == item['sha256']\n"
            "print(f\"verified {len(manifest['files'])} delivery artifacts\")\n"
        )
        compile(source, "verify_delivery.ipynb", "exec")
        return canonical_json_bytes(
            {
                "cells": [
                    {
                        "cell_type": "markdown",
                        "metadata": {},
                        "source": [
                            "# SciDataFusion delivery verification\n",
                            "Run this notebook from the extracted package directory.\n",
                        ],
                    },
                    {
                        "cell_type": "code",
                        "execution_count": None,
                        "metadata": {},
                        "outputs": [],
                        "source": source.splitlines(keepends=True),
                    },
                ],
                "metadata": {
                    "kernelspec": {
                        "display_name": "Python 3",
                        "language": "python",
                        "name": "python3",
                    },
                    "language_info": {"name": "python", "version": "3.11"},
                },
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        )


class ReproducibilityPackager:
    """Create a byte-for-byte deterministic UTF-8 ZIP archive."""

    def build(self, files: dict[str, bytes], manifest_bytes: bytes, maximum_bytes: int) -> bytes:
        entries = {**files, "manifest.json": manifest_bytes}
        if len(entries) != len(files) + 1:
            raise AppError(ErrorCode.VALIDATION_FAILED, "M20 package filename collision")
        buffer = io.BytesIO()
        with zipfile.ZipFile(
            buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for filename in sorted(entries):
                info = zipfile.ZipInfo(filename, date_time=_ZIP_TIMESTAMP)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                info.flag_bits |= 0x800
                archive.writestr(info, entries[filename])
        payload = buffer.getvalue()
        if len(payload) > maximum_bytes:
            raise AppError(ErrorCode.VALIDATION_FAILED, "M20 reproduction package is too large")
        self.verify(payload, entries)
        return payload

    def verify(self, payload: bytes, expected: dict[str, bytes]) -> None:
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                names = archive.namelist()
                if names != sorted(expected) or any(name.startswith(("/", "\\")) for name in names):
                    raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, "unsafe M20 ZIP manifest")
                for name, content in expected.items():
                    if archive.read(name) != content:
                        raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, "M20 ZIP bytes changed")
        except zipfile.BadZipFile as exc:
            raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, "invalid M20 ZIP package") from exc


def parse_notebook(payload: bytes) -> dict[str, object]:
    value = json.loads(payload)
    if not isinstance(value, dict) or value.get("nbformat") != 4:
        raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, "invalid M20 notebook")
    return value
