"""Typed runtime configuration loaded from environment variables."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, HttpUrl, SecretStr, StringConstraints, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class BailianRegion(StrEnum):
    CN_BEIJING = "cn-beijing"
    US_VIRGINIA = "us-virginia"
    AP_SINGAPORE = "ap-southeast-1"
    AP_TOKYO = "ap-northeast-1"


WorkspaceId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Settings(BaseSettings):
    """Application settings with offline-safe defaults."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="SCIDATA_",
        case_sensitive=False,
        env_ignore_empty=True,
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = "SciDataFusion"
    environment: Environment = Environment.LOCAL
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    data_dir: Path = Path("var")
    offline_mode: bool = True

    dashscope_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("DASHSCOPE_API_KEY", "SCIDATA_DASHSCOPE_API_KEY"),
        repr=False,
    )
    bailian_region: BailianRegion = BailianRegion.CN_BEIJING
    bailian_workspace_id: WorkspaceId | None = None
    qwen_base_url_override: HttpUrl | None = Field(
        default=None,
        validation_alias=AliasChoices("SCIDATA_QWEN_BASE_URL", "SCIDATA_QWEN_BASE_URL_OVERRIDE"),
    )
    planner_model_id: str = "qwen-plus"
    fast_model_id: str = "qwen-turbo"
    critic_model_id: str = "qwen-plus"
    model_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    model_max_retries: int = Field(default=2, ge=0, le=8)
    model_max_concurrency: int = Field(default=4, ge=1, le=64)

    default_max_sources: int = Field(default=50, ge=1, le=1000)
    default_max_download_bytes: int = Field(default=500 * 1024 * 1024, ge=1)
    default_model_token_budget: int = Field(default=100_000, ge=1)

    @model_validator(mode="after")
    def require_credentials_for_online_mode(self) -> Settings:
        if self.offline_mode:
            return self
        problems: list[str] = []
        if self.dashscope_api_key is None:
            problems.append("DASHSCOPE_API_KEY is required")
        if self.resolved_qwen_base_url is None:
            problems.append("SCIDATA_BAILIAN_WORKSPACE_ID is required for this region")
        if self.qwen_base_url_override is not None and not self._is_allowed_online_endpoint(
            self.qwen_base_url_override
        ):
            problems.append(
                "SCIDATA_QWEN_BASE_URL must be an official Alibaba Cloud HTTPS endpoint"
            )
        model_ids = (self.planner_model_id, self.fast_model_id, self.critic_model_id)
        if any(not model_id.lower().startswith("qwen") for model_id in model_ids):
            problems.append("online core model roles must use Qwen model IDs")
        if problems:
            msg = "; ".join(problems) + " when SCIDATA_OFFLINE_MODE=false"
            raise ValueError(msg)
        return self

    @property
    def resolved_qwen_base_url(self) -> str | None:
        """Resolve the regional Bailian OpenAI-compatible API base URL."""

        if self.qwen_base_url_override is not None:
            return str(self.qwen_base_url_override).rstrip("/")
        if self.bailian_region is BailianRegion.US_VIRGINIA:
            return "https://dashscope-us.aliyuncs.com/compatible-mode/v1"
        if self.bailian_workspace_id is None:
            return None
        hosts = {
            BailianRegion.CN_BEIJING: "cn-beijing.maas.aliyuncs.com",
            BailianRegion.AP_SINGAPORE: "ap-southeast-1.maas.aliyuncs.com",
            BailianRegion.AP_TOKYO: "ap-northeast-1.maas.aliyuncs.com",
        }
        host = hosts[self.bailian_region]
        return f"https://{self.bailian_workspace_id}.{host}/compatible-mode/v1"

    @staticmethod
    def _is_allowed_online_endpoint(endpoint: HttpUrl) -> bool:
        host = endpoint.host or ""
        allowed_legacy_hosts = {
            "dashscope-us.aliyuncs.com",
            "dashscope.aliyuncs.com",
            "dashscope-intl.aliyuncs.com",
        }
        return endpoint.scheme == "https" and (
            host in allowed_legacy_hosts or host.endswith(".maas.aliyuncs.com")
        )

    def diagnostic_summary(self) -> dict[str, object]:
        """Return operational settings without exposing credential material."""

        return {
            "app_name": self.app_name,
            "environment": self.environment.value,
            "log_level": self.log_level,
            "data_dir": str(self.data_dir),
            "offline_mode": self.offline_mode,
            "bailian_region": self.bailian_region.value,
            "qwen_base_url": self.resolved_qwen_base_url,
            "planner_model_id": self.planner_model_id,
            "credentials_configured": self.dashscope_api_key is not None,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and cache process-wide settings."""

    return Settings()
