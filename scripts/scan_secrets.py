"""Fail when common credential material appears in project files."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_DIRS = {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "var"}
EXCLUDED_FILES = {Path(__file__).resolve(), ROOT / "uv.lock"}
MAX_FILE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class SecretPattern:
    name: str
    regex: re.Pattern[str]


PATTERNS = (
    SecretPattern(
        "private_key", re.compile("-----BEGIN " + r"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
    ),
    SecretPattern("openai_style_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    SecretPattern("aliyun_access_key", re.compile(r"\b" + "LTAI" + r"[A-Za-z0-9]{12,}\b")),
    SecretPattern(
        "populated_env_secret",
        re.compile(
            r"(?im)^(?:[A-Z0-9_]*(?:API_KEY|ACCESS_TOKEN|PASSWORD|SECRET|TOKEN))"
            r"[ \t]*=[ \t]*[^\s#][^\r\n]*$"
        ),
    ),
)


def iter_text_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.resolve() in EXCLUDED_FILES:
            continue
        if any(part in EXCLUDED_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            continue
        files.append(path)
    return files


def main() -> int:
    findings: list[str] = []
    for path in iter_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for pattern in PATTERNS:
            if pattern.regex.search(text):
                findings.append(f"{path.relative_to(ROOT)}: {pattern.name}")
    if findings:
        print("Potential secrets detected:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        return 1
    print(f"Secret scan passed ({len(iter_text_files())} files checked).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
