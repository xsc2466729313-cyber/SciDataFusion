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


def test_phase1_demo_prints_confirmed_safe_summary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    goal = "Integrate multi-source Type Ia supernova light curves into CSV."
    reviewer = "private-reviewer@example.org"

    exit_code = main(
        [
            "phase1-demo",
            "--goal",
            goal,
            "--confirmed-by",
            reviewer,
        ]
    )
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 0
    assert report["status"] == "confirmed"
    assert report["simulated_capabilities"] is True
    assert report["routing"]["primary_domain"] == "astronomy"
    assert set(report["routing"]["task_packs"]) == {"data_integration", "light_curve"}
    assert report["contract"]["status"] == "confirmed"
    assert report["contract"]["output_formats"] == ["csv"]
    assert goal not in captured.out
    assert reviewer not in captured.out


def test_phase1_demo_review_exit_and_validation_error_are_structured(
    capsys: pytest.CaptureFixture[str],
) -> None:
    review_exit = main(["phase1-demo", "--goal", "data"])
    review_output = json.loads(capsys.readouterr().out)

    assert review_exit == 3
    assert review_output["status"] == "needs_review"
    assert review_output["issues"][0]["blocking"] is True

    invalid_exit = main(["phase1-demo", "--goal", ""])
    invalid_output = json.loads(capsys.readouterr().err)

    assert invalid_exit == 2
    assert invalid_output == {"status": "error", "error": "validation_failed"}
