"""Windows-friendly local launcher for the SciDataFusion workbench."""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser
from collections.abc import Sequence
from pathlib import Path

import httpx
import uvicorn

from scidatafusion.api import app

LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
MAX_PORT = 8099


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def runtime_directory() -> Path:
    """Return the writable directory beside a frozen executable."""

    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path.cwd().resolve()


def choose_available_port(preferred: int = DEFAULT_PORT) -> int:
    """Choose the first available loopback port in the bounded desktop range."""

    if not DEFAULT_PORT <= preferred <= MAX_PORT:
        raise ValueError(f"port must be between {DEFAULT_PORT} and {MAX_PORT}")
    for port in range(preferred, MAX_PORT + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((LOOPBACK_HOST, port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"no available local port between {preferred} and {MAX_PORT}")


def _open_when_ready(url: str, *, timeout_seconds: float = 90.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    health_url = f"{url}/api/health"
    while time.monotonic() < deadline:
        try:
            response = httpx.get(health_url, timeout=1.0)
            if response.status_code == 200:
                webbrowser.open(url)
                return
        except httpx.HTTPError:
            time.sleep(0.25)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the local SciDataFusion workbench")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    return parser


def _serve(port: int) -> None:
    uvicorn.run(app, host=LOOPBACK_HOST, port=port, log_level="info")


def main(argv: Sequence[str] | None = None) -> int:
    """Start one loopback-only server and optionally open the default browser."""

    arguments = _parser().parse_args(argv)
    workdir = runtime_directory()
    os.chdir(workdir)
    port = choose_available_port(arguments.port)
    url = f"http://{LOOPBACK_HOST}:{port}"
    print(f"SciDataFusion is starting at {url}", flush=True)
    print("Keep this window open. Press Ctrl+C to stop the service.", flush=True)
    if not arguments.no_browser:
        threading.Thread(target=_open_when_ready, args=(url,), daemon=True).start()
    _serve(port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
