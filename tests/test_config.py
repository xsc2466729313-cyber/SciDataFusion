from pathlib import Path

import pytest
from pydantic import ValidationError

from scidatafusion.config import BailianRegion, Environment, PlatformMode, Settings, get_settings


def test_defaults_are_offline_and_secret_safe(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path)

    summary = settings.diagnostic_summary()
    assert settings.environment is Environment.LOCAL
    assert settings.offline_mode is True
    assert summary["credentials_configured"] is False
    assert summary["serpapi_configured"] is False
    assert "api_key" not in summary


def test_get_settings_reloads_persistent_local_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration_file = tmp_path / "online.env"
    configuration_file.write_text(
        "\n".join(
            (
                "SCIDATA_OFFLINE_MODE=false",
                "DASHSCOPE_API_KEY=test-dashscope-key-material",
                "SERPAPI_API_KEY=test-serpapi-key-material",
                "SCIDATA_QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SCIDATA_LOCAL_CONFIGURATION_FILE", str(configuration_file))
    monkeypatch.delenv("SCIDATA_OFFLINE_MODE", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    get_settings.cache_clear()

    try:
        settings = get_settings()
        assert settings.offline_mode is False
        assert settings.dashscope_api_key is not None
        assert settings.serpapi_api_key is not None
        assert settings.local_configuration_file == configuration_file
    finally:
        get_settings.cache_clear()


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


def test_beijing_shared_endpoint_does_not_require_workspace_id_online() -> None:
    settings = Settings(
        _env_file=None,
        offline_mode=False,
        dashscope_api_key="test-key-material",
        bailian_region=BailianRegion.CN_BEIJING,
    )

    assert settings.resolved_qwen_base_url == ("https://dashscope.aliyuncs.com/compatible-mode/v1")


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


def test_online_search_strategy_is_bounded_and_normalized() -> None:
    settings = Settings(
        _env_file=None,
        search_engine="google_scholar",
        search_language="ZH-CN",
        search_country="CN",
        search_max_queries=4,
    )

    assert settings.search_engine == "google_scholar"
    assert settings.search_language == "zh-cn"
    assert settings.search_country == "cn"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, search_max_queries=7)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, search_country="china")


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCIDATA_OFFLINE_MODE", "true")
    get_settings.cache_clear()


def test_platform_defaults_are_local_and_secret_safe() -> None:
    settings = Settings(_env_file=None)

    summary = settings.diagnostic_summary()
    assert settings.platform_mode is PlatformMode.LOCAL
    assert summary["postgres_configured"] is False
    assert summary["redis_configured"] is False
    assert summary["chroma_host"] is None
    assert "database_url" not in str(summary)


def test_celery_platform_requires_all_infrastructure_urls() -> None:
    with pytest.raises(ValidationError, match="SCIDATA_DATABASE_URL"):
        Settings(_env_file=None, platform_mode="celery")

    settings = Settings(
        _env_file=None,
        platform_mode="celery",
        database_url="postgresql://user:secret@postgres:5432/scidatafusion",
        redis_url="redis://:secret@redis:6379/0",
        chroma_url="http://chroma:8000",
    )
    summary = settings.diagnostic_summary()
    assert summary["postgres_configured"] is True
    assert summary["redis_configured"] is True
    assert summary["chroma_host"] == "chroma"
    assert "secret" not in str(summary)

    assert get_settings() is get_settings()

    get_settings.cache_clear()
