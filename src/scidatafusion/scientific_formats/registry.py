"""M12 parser-plugin registry with exact runtime binding."""

from __future__ import annotations

from typing import cast

from scidatafusion.contracts.datasets import ScientificParserDescriptor, ScientificRuntimeSnapshot
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.scientific_formats.base import ScientificFormatParser
from scidatafusion.scientific_formats.fits import FitsParser


class PluginParserRegistry:
    """Own parser construction instead of branching on scientific domains."""

    def __init__(self, parsers: tuple[ScientificFormatParser, ...] | None = None) -> None:
        if parsers is None:
            values: tuple[ScientificFormatParser, ...] = (
                cast(ScientificFormatParser, FitsParser()),
            )
        else:
            values = parsers
        self._parsers = {item.parser_id: item for item in values}
        if len(self._parsers) != len(values):
            raise AppError(ErrorCode.CONFIGURATION_ERROR, "duplicate M12 parser plugin id")

    def resolve(self, runtime: ScientificRuntimeSnapshot) -> ScientificFormatParser:
        """Return only the plugin pinned by the immutable runtime descriptor."""

        parser = self._parsers.get(runtime.parser.parser_id)
        if parser is None:
            raise AppError(
                ErrorCode.CONFIGURATION_ERROR,
                "M12 parser plugin is unavailable",
                details={"parser_id": runtime.parser.parser_id},
            )
        descriptor: ScientificParserDescriptor = runtime.parser
        if not (
            parser.parser_version == descriptor.parser_version
            and parser.engine_name == descriptor.engine_name
            and parser.engine_version == descriptor.engine_version
        ):
            raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, "M12 parser runtime drift detected")
        return parser
