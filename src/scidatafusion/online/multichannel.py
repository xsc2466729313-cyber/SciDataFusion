"""Dispatch planned searches to their allowlisted provider clients."""

from __future__ import annotations

from typing import Protocol

from scidatafusion.config import Settings
from scidatafusion.contracts.online import LiveSearchBatch, SearchChannel
from scidatafusion.online.arxiv import ArxivSearchClient
from scidatafusion.online.search import SerpApiSearchClient


class ChannelSearchClient(Protocol):
    async def search(self, query: str, channel: SearchChannel) -> LiveSearchBatch: ...


class MultiChannelSearchClient:
    def __init__(
        self,
        settings: Settings,
        *,
        serpapi_client: ChannelSearchClient | None = None,
        arxiv_client: ChannelSearchClient | None = None,
    ) -> None:
        self._serpapi = serpapi_client or SerpApiSearchClient(settings)
        self._arxiv = arxiv_client or ArxivSearchClient(settings)

    async def search(self, query: str, channel: SearchChannel) -> LiveSearchBatch:
        if channel is SearchChannel.ARXIV:
            return await self._arxiv.search(query, channel)
        return await self._serpapi.search(query, channel)
