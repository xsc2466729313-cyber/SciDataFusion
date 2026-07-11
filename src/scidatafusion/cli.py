"""Local operational commands for the engineering baseline."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from scidatafusion import __version__
from scidatafusion.config import Settings


def build_doctor_report(settings: Settings) -> dict[str, object]:
    """Check local runtime prerequisites without making network requests."""

    data_dir = Path(settings.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    return {
        "status": "ok",
        "package": "scidatafusion",
        "version": __version__,
        "python": ".".join(str(part) for part in sys.version_info[:3]),
        "data_dir_exists": data_dir.is_dir(),
        "settings": settings.diagnostic_summary(),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scidatafusion")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="validate local configuration without network calls")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "doctor":
        try:
            report = build_doctor_report(Settings())
        except ValidationError as exc:
            print(
                json.dumps(
                    {
                        "status": "error",
                        "error": "invalid_configuration",
                        "details": exc.errors(include_context=False, include_input=False),
                    },
                    ensure_ascii=True,
                ),
                file=sys.stderr,
            )
            return 2
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return 0
    return 2
