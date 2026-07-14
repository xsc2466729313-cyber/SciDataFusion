"""Desktop launcher tests."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from scidatafusion import desktop


def test_choose_available_port_skips_an_occupied_port() -> None:
    first_available = desktop.choose_available_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind((desktop.LOOPBACK_HOST, first_available))
        assert desktop.choose_available_port(first_available) > first_available


def test_choose_available_port_rejects_unbounded_values() -> None:
    with pytest.raises(ValueError, match="port must be between"):
        desktop.choose_available_port(7999)


def test_development_runtime_directory_is_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(desktop, "_is_frozen", lambda: False)
    assert desktop.runtime_directory() == tmp_path.resolve()


def test_main_starts_loopback_server_without_opening_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(desktop, "choose_available_port", lambda preferred: preferred)
    monkeypatch.setattr(desktop, "_serve", calls.append)
    assert desktop.main(("--port", "8008", "--no-browser")) == 0
    assert calls == [8008]
