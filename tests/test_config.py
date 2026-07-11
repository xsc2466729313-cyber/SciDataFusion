from pathlib import Path

import pytest
from pydantic import ValidationError

from scidatafusion.config import BailianRegion, Environment, Settings, get_settings


def test_defaults_are_offline_and_secret_safe(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path)

    summary = settings.diagnostic_summary()
    assert settings.environment is Environment.LOCAL
    assert settings.offline_mode is True
    assert summary["credentials_configured"] is False
    assert "api_key" not in summary


def test_secret_value_never_appears_in_repr_or_diagnostics(tmp_path: Path) -> None:
    secret = "competition-secret-value"
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        dashscope_api_key=secret,
    )

    assert secret not in repr(settings)
    assert secret not in str(settings.diagnostic_summary())
    assert settings.diagnostic_summary()["credentials_configured"] is True


def test_online_mode_requires_dashscope_key() -> None:
    with pytest.raises(ValidationError, match="DASHSCOPE_API_KEY"):
        Settings(_env_file=None, offline_mode=False, dashscope_api_key=None)


def test_budget_limits_are_validated() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, default_max_sources=0)


def test_regional_bailian_endpoint_is_derived_from_workspace() -> None:
    settings = Settings(
        _env_file=None,
        bailian_region=BailianRegion.AP_SINGAPORE,
        bailian_workspace_id="ws-demo",
    )

    assert settings.resolved_qwen_base_url == (
        "https://ws-demo.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"
    )


def test_us_region_does_not_require_workspace_id_online() -> None:
    settings = Settings(
        _env_file=None,
        offline_mode=False,
        dashscope_api_key="test-key-material",
        bailian_region=BailianRegion.US_VIRGINIA,
    )

    assert settings.resolved_qwen_base_url == (
        "https://dashscope-us.aliyuncs.com/compatible-mode/v1"
    )


def test_online_mode_rejects_non_aliyun_endpoint() -> None:
    with pytest.raises(ValidationError, match="official Alibaba Cloud"):
        Settings(
            _env_file=None,
            offline_mode=False,
            dashscope_api_key="test-key-material",
            qwen_base_url_override="https://example.com/v1",
        )


def test_online_mode_rejects_non_qwen_core_model() -> None:
    with pytest.raises(ValidationError, match="Qwen model IDs"):
        Settings(
            _env_file=None,
            offline_mode=False,
            dashscope_api_key="test-key-material",
            bailian_region=BailianRegion.US_VIRGINIA,
            planner_model_id="other-model",
        )


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCIDATA_OFFLINE_MODE", "true")
    get_settings.cache_clear()

    assert get_settings() is get_settings()

    get_settings.cache_clear()
