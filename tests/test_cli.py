import json
from pathlib import Path

import pytest

from scidatafusion.cli import build_doctor_report, main
from scidatafusion.config import Settings


def test_doctor_report_creates_data_directory(tmp_path: Path) -> None:
    data_dir = tmp_path / "runtime"
    settings = Settings(_env_file=None, data_dir=data_dir)

    report = build_doctor_report(settings)

    assert report["status"] == "ok"
    assert report["data_dir_exists"] is True
    assert data_dir.is_dir()


def test_doctor_command_prints_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SCIDATA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCIDATA_OFFLINE_MODE", "true")

    exit_code = main(["doctor"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert json.loads(captured.out)["status"] == "ok"


def test_doctor_command_reports_invalid_configuration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SCIDATA_OFFLINE_MODE", "false")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("SCIDATA_DASHSCOPE_API_KEY", raising=False)

    exit_code = main(["doctor"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert json.loads(captured.err)["error"] == "invalid_configuration"


def test_configuration_error_does_not_echo_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel = "do-not-print-this-key"
    monkeypatch.setenv("SCIDATA_OFFLINE_MODE", "false")
    monkeypatch.setenv("DASHSCOPE_API_KEY", sentinel)
    monkeypatch.setenv("SCIDATA_QWEN_BASE_URL", "https://example.com/v1")

    exit_code = main(["doctor"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert sentinel not in captured.err
