"""Automatic, bounded acquisition of AI-approved live source materials."""

from __future__ import annotations

import io
import zipfile
from collections import deque
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from defusedxml import ElementTree
from pydantic import HttpUrl

from scidatafusion.artifacts.downloader import (
    DnsPinnedTransport,
    DownloadFailure,
    SafeDownloadClient,
)
from scidatafusion.artifacts.integrity import (
    calculate_download_runtime_hash,
    calculate_url_locator_hash,
)
from scidatafusion.artifacts.sniffer import ContentSniffer
from scidatafusion.artifacts.storage import BronzeByteStore, FileSystemBronzeStore
from scidatafusion.contracts.artifacts import (
    DownloadExecutionMode,
    DownloadPolicy,
    DownloadRuntimeSnapshot,
)
from scidatafusion.contracts.base import utc_now
from scidatafusion.contracts.online import (
    ArtifactReviewInput,
    OnlineAcquiredArtifact,
    OnlineAcquisitionFailure,
    OnlineAcquisitionResult,
    OnlineResearchResult,
)
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.errors import AppError
from scidatafusion.online.repository import DuckDBOnlineArtifactRepository

_MAX_FILES = 5
_AUTO_PROMOTION_LIMIT = 2
_MAX_FILE_BYTES = 10_000_000
_MAX_TOTAL_BYTES = 25_000_000
_DIRECT_ARTIFACT_SUFFIXES = (
    ".csv",
    ".fits",
    ".geojson",
    ".grb",
    ".grib",
    ".h5",
    ".hdf5",
    ".json",
    ".nc",
    ".parquet",
    ".pdf",
    ".tif",
    ".tiff",
    ".tsv",
    ".txt",
    ".xls",
    ".xlsx",
    ".zip",
)
_MACHINE_READABLE_SUFFIXES = tuple(
    suffix for suffix in _DIRECT_ARTIFACT_SUFFIXES if suffix not in {".pdf", ".txt"}
)
_MAX_LINKS_PER_LANDING = 2


class OnlineAcquisitionService:
    """Download exact search-result URLs selected by the validated AI assessment."""

    def __init__(
        self,
        *,
        store: BronzeByteStore | None = None,
        transport_factory: Callable[[tuple[str, ...]], DnsPinnedTransport] | None = None,
        repository: DuckDBOnlineArtifactRepository | None = None,
    ) -> None:
        self._store = store or FileSystemBronzeStore(Path("var/online-bronze"))
        self._transport_factory = transport_factory
        self._repository = repository or DuckDBOnlineArtifactRepository()

    def read_artifact(self, byte_sha256: str) -> bytes:
        """Replay one acquired object after the Bronze store verifies its content hash."""

        return self._store.read(byte_sha256)

    def build_review_inputs(
        self, artifacts: tuple[OnlineAcquiredArtifact, ...]
    ) -> tuple[ArtifactReviewInput, ...]:
        """Build bounded, non-authoritative previews for semantic material review."""

        return tuple(
            ArtifactReviewInput(
                byte_sha256=artifact.byte_sha256,
                source_url=artifact.source_url,
                source_title=artifact.source_title,
                media_type=artifact.media_type,
                artifact_kind=artifact.artifact_kind,
                content_preview=_content_preview(
                    self.read_artifact(artifact.byte_sha256), artifact
                ),
            )
            for artifact in artifacts
        )

    async def acquire(self, research: OnlineResearchResult) -> OnlineAcquisitionResult:
        explicitly_selected = tuple(
            item
            for item in research.sources
            if item.assessment is not None
            and item.assessment.recommended_action == "download"
            and urlsplit(str(item.search.url)).scheme == "https"
        )
        explicit_urls = {str(item.search.url) for item in explicitly_selected}
        inspect_candidates = tuple(
            item
            for item in research.sources
            if item.assessment is not None
            and item.assessment.recommended_action == "inspect"
            and urlsplit(str(item.search.url)).scheme == "https"
            and str(item.search.url) not in explicit_urls
        )
        promoted = tuple(
            sorted(
                inspect_candidates,
                key=lambda item: (
                    not urlsplit(str(item.search.url))
                    .path.casefold()
                    .endswith(_DIRECT_ARTIFACT_SUFFIXES),
                    -item.assessment.relevance_score if item.assessment is not None else 0.0,
                ),
            )[:_AUTO_PROMOTION_LIMIT]
        )
        selected = (explicitly_selected + promoted)[:_MAX_FILES]
        hosts = tuple(
            dict.fromkeys(
                (urlsplit(str(item.search.url)).hostname or "").casefold().rstrip(".")
                for item in selected
            )
        )
        policy = DownloadPolicy(
            policy_version="1.1.0",
            max_total_bytes=_MAX_TOTAL_BYTES,
            max_file_bytes=_MAX_FILE_BYTES,
            max_redirects=0,
            connect_timeout_seconds=5.0,
            read_timeout_seconds=10.0,
            max_attempts=1,
            requests_per_second_per_host=1.0,
        )
        policy_hash = canonical_hash(policy.model_dump(mode="json"))
        if not selected:
            result = OnlineAcquisitionResult(
                attempted_count=0,
                artifacts=(),
                failures=(),
                allowed_hosts=(),
                policy_hash=policy_hash,
            )
            return result.model_copy(update={"catalog": self._repository.persist(result)})
        checked_at = utc_now()
        runtime = DownloadRuntimeSnapshot(
            execution_mode=DownloadExecutionMode.LIVE_NETWORK,
            network_enabled=True,
            allowed_hosts=hosts,
            checked_at=checked_at,
            runtime_hash="0" * 64,
        )
        runtime = runtime.model_copy(
            update={"runtime_hash": calculate_download_runtime_hash(runtime)}
        )
        artifacts: list[OnlineAcquiredArtifact] = []
        failures: list[OnlineAcquisitionFailure] = []
        remaining = _MAX_TOTAL_BYTES
        pending = deque(
            (str(item.search.url), item.search.title, item.search.url) for item in selected
        )
        seen_urls: set[str] = set()
        transport = None if self._transport_factory is None else self._transport_factory(hosts)
        async with SafeDownloadClient(runtime, policy, transport=transport) as client:
            while pending and len(artifacts) + len(failures) < _MAX_FILES:
                url, title, source_url = pending.popleft()
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                locator_hash = calculate_url_locator_hash(url)
                try:
                    fetched = await client.fetch(
                        url,
                        byte_limit=min(_MAX_FILE_BYTES, remaining),
                        approved_locator_hashes=frozenset({locator_hash}),
                    )
                    inspection = ContentSniffer.inspect(
                        fetched.content,
                        declared_media_type=fetched.response.declared_content_type,
                    )
                    receipt = self._store.put(fetched.content)
                    remaining -= receipt.size_bytes
                    artifacts.append(
                        OnlineAcquiredArtifact(
                            source_url=source_url,
                            source_title=title,
                            locator_hash=locator_hash,
                            byte_sha256=receipt.byte_sha256,
                            size_bytes=receipt.size_bytes,
                            media_type=inspection.detected_media_type,
                            artifact_kind=inspection.artifact_kind.value,
                            storage_uri=receipt.storage_uri,
                        )
                    )
                    if inspection.artifact_kind.value == "landing_page":
                        links = _direct_attachment_urls(fetched.content, url)
                        for link in reversed(links):
                            pending.appendleft((link, f"{title} attachment"[:512], HttpUrl(link)))
                except DownloadFailure as exc:
                    failures.append(
                        OnlineAcquisitionFailure(
                            source_url=source_url,
                            source_title=title,
                            locator_hash=locator_hash,
                            error_code=exc.code.value,
                            retryable=exc.retryable,
                        )
                    )
                except AppError as exc:
                    failures.append(
                        OnlineAcquisitionFailure(
                            source_url=source_url,
                            source_title=title,
                            locator_hash=locator_hash,
                            error_code=exc.code.value,
                            retryable=exc.retryable,
                        )
                    )
        result = OnlineAcquisitionResult(
            attempted_count=len(artifacts) + len(failures),
            artifacts=tuple(artifacts),
            failures=tuple(failures),
            allowed_hosts=hosts,
            policy_hash=policy_hash,
        )
        return result.model_copy(update={"catalog": self._repository.persist(result)})


class _HrefCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() not in {"a", "link"}:
            return
        for name, value in attrs:
            if name.casefold() == "href" and value:
                self.hrefs.append(value.strip())


def _direct_attachment_urls(content: bytes, base_url: str) -> tuple[str, ...]:
    """Extract a bounded set of same-host direct data links from untrusted HTML bytes."""

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ()
    collector = _HrefCollector()
    try:
        collector.feed(text)
    except ValueError:
        return ()
    base = urlsplit(base_url)
    results: list[str] = []
    for href in collector.hrefs:
        candidate = urljoin(base_url, href)
        parsed = urlsplit(candidate)
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.hostname != base.hostname
            or not parsed.path.casefold().endswith(_MACHINE_READABLE_SUFFIXES)
        ):
            continue
        normalized = parsed._replace(fragment="").geturl()
        if normalized not in results:
            results.append(normalized)
        if len(results) >= _MAX_LINKS_PER_LANDING:
            break
    return tuple(results)


def _content_preview(content: bytes, artifact: OnlineAcquiredArtifact) -> str:
    media_type = artifact.media_type.casefold()
    if media_type.startswith("text/") or media_type in {"application/json", "application/geo+json"}:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = ""
        return _bounded_preview(text, artifact)
    if media_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                shared = archive.read("xl/sharedStrings.xml")
            root = ElementTree.fromstring(shared)
            values = [node.text or "" for node in root.iter() if node.tag.endswith("}t")]
            return _bounded_preview(" | ".join(values), artifact)
        except (KeyError, OSError, ValueError, zipfile.BadZipFile):
            pass
    if media_type in {"application/zip", "application/gzip"}:
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                member_names = archive.namelist()[:80]
                samples: list[str] = []
                for entry in archive.infolist():
                    suffix = Path(entry.filename).suffix.casefold()
                    if (
                        entry.is_dir()
                        or entry.file_size > _MAX_FILE_BYTES
                        or suffix not in {".csv", ".tsv", ".json", ".geojson", ".txt"}
                    ):
                        continue
                    with archive.open(entry) as stream:
                        sample = stream.read(1024).decode("utf-8-sig", errors="replace")
                    normalized_sample = " ".join(sample.split())
                    if normalized_sample:
                        samples.append(f"sample {entry.filename}: {normalized_sample}")
                    if len(samples) >= 2:
                        break
                preview = "archive members: " + " | ".join(member_names)
                if samples:
                    preview += "; embedded records: " + " || ".join(samples)
                return _bounded_preview(preview, artifact)
        except (OSError, RuntimeError, zipfile.BadZipFile):
            pass
    return _bounded_preview(
        f"binary scientific material; media={artifact.media_type}; bytes={artifact.size_bytes}",
        artifact,
    )


def _bounded_preview(value: str, artifact: OnlineAcquiredArtifact) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        normalized = f"no textual preview; media={artifact.media_type}; bytes={artifact.size_bytes}"
    return normalized[:2048]
