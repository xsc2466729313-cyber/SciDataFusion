"""Deterministic evidence embeddings and optional Chroma/framework adapters."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import math
import re
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

from scidatafusion.contracts.platform import EvidenceVectorDocument, VectorIndexReport

_TOKEN_PATTERN = re.compile(r"(?u)\b\w\w+\b")


class EvidenceVectorizer:
    """Hash evidence text without fitting on or inventing scientific values."""

    def __init__(self, dimensions: int = 384) -> None:
        self.dimensions = dimensions

    def encode(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        try:
            feature_extraction = importlib.import_module("sklearn.feature_extraction.text")
        except ModuleNotFoundError:
            vectors = tuple(self._python_hash(text) for text in texts)
        else:
            vectorizer = feature_extraction.HashingVectorizer(
                n_features=self.dimensions,
                alternate_sign=True,
                norm="l2",
            )
            matrix = vectorizer.transform(texts)
            vectors = tuple(tuple(float(value) for value in row) for row in matrix.toarray())
        return self._torch_validate(vectors)

    @property
    def engine(self) -> str:
        try:
            importlib.import_module("sklearn.feature_extraction.text")
        except ModuleNotFoundError:
            return "python-hashing"
        return "sklearn-hashing"

    @staticmethod
    def torch_available() -> bool:
        try:
            importlib.import_module("torch")
        except ModuleNotFoundError:
            return False
        return True

    def _python_hash(self, text: str) -> tuple[float, ...]:
        values = [0.0] * self.dimensions
        for token in _TOKEN_PATTERN.findall(text.casefold()):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:8], "big") % self.dimensions
            values[index] += -1.0 if digest[8] & 1 else 1.0
        norm = math.sqrt(sum(value * value for value in values))
        if norm:
            values = [value / norm for value in values]
        return tuple(values)

    @staticmethod
    def _torch_validate(
        vectors: tuple[tuple[float, ...], ...],
    ) -> tuple[tuple[float, ...], ...]:
        try:
            torch = importlib.import_module("torch")
        except ModuleNotFoundError:
            if any(not math.isfinite(value) for vector in vectors for value in vector):
                raise ValueError("embedding contains a non-finite value") from None
            return vectors
        tensor = torch.tensor(vectors, dtype=torch.float32)
        if not bool(torch.isfinite(tensor).all()):
            raise ValueError("embedding contains a non-finite value")
        return tuple(tuple(float(value) for value in row) for row in tensor.tolist())


class ChromaEvidenceIndex:
    """Upsert immutable evidence text and provenance metadata into Chroma."""

    def __init__(self, url: str, *, dimensions: int = 384) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ValueError("Chroma URL must be HTTP(S) with a hostname")
        self._host = parsed.hostname
        self._port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self._ssl = parsed.scheme == "https"
        self._vectorizer = EvidenceVectorizer(dimensions)

    async def index(self, documents: Sequence[EvidenceVectorDocument]) -> VectorIndexReport:
        if not documents:
            return self._report(0, 0, 0)
        vectors = self._vectorizer.encode([item.text for item in documents])
        langchain_count, llamaindex_count = _framework_document_counts(documents)
        await asyncio.to_thread(self._upsert, documents, vectors)
        return self._report(len(documents), langchain_count, llamaindex_count)

    def _upsert(
        self,
        documents: Sequence[EvidenceVectorDocument],
        vectors: tuple[tuple[float, ...], ...],
    ) -> None:
        chromadb = importlib.import_module("chromadb")
        client = chromadb.HttpClient(host=self._host, port=self._port, ssl=self._ssl)
        collection = client.get_or_create_collection(name="scidatafusion_evidence")
        collection.upsert(
            ids=[item.document_id for item in documents],
            documents=[item.text for item in documents],
            embeddings=[list(vector) for vector in vectors],
            metadatas=[
                {
                    "evidence_id": item.evidence_id,
                    "task_id": item.task_id,
                    "source_hash": item.source_hash,
                    "field_name": item.field_name,
                    "location": item.location,
                }
                for item in documents
            ],
        )

    def _report(self, count: int, langchain_count: int, llamaindex_count: int) -> VectorIndexReport:
        return VectorIndexReport(
            indexed_count=count,
            dimensions=self._vectorizer.dimensions,
            engine=self._vectorizer.engine,  # type: ignore[arg-type]
            torch_validated=self._vectorizer.torch_available(),
            langchain_document_count=langchain_count,
            llamaindex_node_count=llamaindex_count,
        )


def _framework_document_counts(documents: Sequence[EvidenceVectorDocument]) -> tuple[int, int]:
    """Build native framework views when installed; the views never mutate evidence."""

    langchain_count = 0
    llamaindex_count = 0
    try:
        langchain_documents = importlib.import_module("langchain_core.documents")
    except ModuleNotFoundError:
        pass
    else:
        native = [
            langchain_documents.Document(
                page_content=item.text,
                metadata={"evidence_id": item.evidence_id, "source_hash": item.source_hash},
            )
            for item in documents
        ]
        langchain_count = len(native)
    try:
        llama_schema = importlib.import_module("llama_index.core.schema")
    except ModuleNotFoundError:
        pass
    else:
        nodes = [
            llama_schema.TextNode(
                text=item.text,
                id_=item.document_id,
                metadata={"evidence_id": item.evidence_id, "source_hash": item.source_hash},
            )
            for item in documents
        ]
        llamaindex_count = len(nodes)
    return langchain_count, llamaindex_count


def build_evidence_documents(snapshot: Any) -> tuple[EvidenceVectorDocument, ...]:
    """Project only already-evidenced workbench values into vector documents."""

    return tuple(
        EvidenceVectorDocument(
            document_id=hashlib.sha256(
                f"{snapshot.task_id}:{item.evidence_id}:{item.source_hash}".encode()
            ).hexdigest(),
            evidence_id=item.evidence_id,
            task_id=snapshot.task_id,
            text=(
                f"field={item.field_name}\nvalue={item.raw_value}\n"
                f"location={item.source_location}\nmethod={item.method}"
            ),
            source_hash=item.source_hash,
            field_name=item.field_name,
            location=item.source_location,
        )
        for item in snapshot.evidence
    )
