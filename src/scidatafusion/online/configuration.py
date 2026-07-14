"""Validated local `.env` persistence for the browser configuration form."""

from __future__ import annotations

import os
import re
from pathlib import Path

from scidatafusion.config import Settings
from scidatafusion.contracts.online import OnlineConfigurationUpdate

_ASSIGNMENT = re.compile(r"^(?P<name>[A-Z][A-Z0-9_]*)=")


class LocalOnlineConfigurationStore:
    """Persist only the allowlisted online settings and never return secret values."""

    def __init__(self, path: Path) -> None:
        self._path = path.resolve()

    def save(self, update: OnlineConfigurationUpdate) -> Settings:
        values = self._non_secret_values(update)
        if update.clear_serpapi_api_key:
            values["SERPAPI_API_KEY"] = ""
        elif update.serpapi_api_key is not None:
            values["SERPAPI_API_KEY"] = update.serpapi_api_key.get_secret_value()
        if update.clear_dashscope_api_key:
            values["DASHSCOPE_API_KEY"] = ""
        elif update.dashscope_api_key is not None:
            values["DASHSCOPE_API_KEY"] = update.dashscope_api_key.get_secret_value()

        existing = self._path.read_text(encoding="utf-8") if self._path.exists() else ""
        content = self._merge(existing, values)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.parent / f".{self._path.name}.scidatafusion.tmp"
        try:
            temporary.write_text(content, encoding="utf-8", newline="\n")
            settings = Settings(_env_file=temporary)
            os.replace(temporary, self._path)
        finally:
            temporary.unlink(missing_ok=True)
        return settings

    @staticmethod
    def _non_secret_values(update: OnlineConfigurationUpdate) -> dict[str, str]:
        return {
            "SCIDATA_OFFLINE_MODE": str(not update.online_enabled).lower(),
            "SCIDATA_QWEN_BASE_URL": str(update.qwen_base_url).rstrip("/"),
            "SCIDATA_BAILIAN_REGION": update.bailian_region,
            "SCIDATA_BAILIAN_WORKSPACE_ID": update.bailian_workspace_id or "",
            "SCIDATA_SEARCH_ENGINE": update.search_engine,
            "SCIDATA_SEARCH_LANGUAGE": update.search_language,
            "SCIDATA_SEARCH_COUNTRY": update.search_country or "",
            "SCIDATA_SEARCH_QUERY_PLANNING_ENABLED": str(update.query_planning_enabled).lower(),
            "SCIDATA_SEARCH_MAX_QUERIES": str(update.max_search_queries),
            "SCIDATA_SEARCH_MAX_RESULTS": str(update.max_search_results),
            "SCIDATA_PLANNER_MODEL_ID": update.planner_model_id,
            "SCIDATA_FAST_MODEL_ID": update.assessment_model_id,
        }

    @staticmethod
    def _merge(existing: str, updates: dict[str, str]) -> str:
        remaining = dict(updates)
        output: list[str] = []
        for line in existing.splitlines():
            match = _ASSIGNMENT.match(line)
            name = None if match is None else match.group("name")
            if name in remaining:
                output.append(f"{name}={remaining.pop(name)}")
            else:
                output.append(line)
        if output and output[-1] != "":
            output.append("")
        output.append("# Managed by the local SciDataFusion configuration form.")
        output.extend(f"{name}={value}" for name, value in remaining.items())
        return "\n".join(output).rstrip() + "\n"
