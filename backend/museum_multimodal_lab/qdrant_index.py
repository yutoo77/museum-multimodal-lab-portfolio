# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

"""Rebuildable, local Qdrant index for the current SQLite chunk snapshot.

SQLite remains authoritative. Each query reconciles one model/dimension
collection against the caller's *currently approved* rows before searching.
No source text is stored in Qdrant payloads.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import uuid
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient, models


class QdrantIndex:
    """Small, single-process persistent vector index derived from SQLite."""

    def __init__(self, data_dir: Path):
        self._client = QdrantClient(path=str(Path(data_dir) / "qdrant"))
        self._lock = threading.RLock()
        self._closed = False

    @staticmethod
    def _collection_name(model: str, dimension: int, space: str = "text") -> str:
        if not model:
            raise ValueError("embedding model must not be empty")
        if space not in {"text", "visual"}:
            raise ValueError("unsupported vector space")
        digest = hashlib.sha256(model.encode("utf-8")).hexdigest()[:24]
        return f"mml_{space}_{digest}_{dimension}"

    @staticmethod
    def _point_id(chunk_id: str) -> str:
        # Qdrant point IDs must be UUIDs or unsigned integers; SQLite IDs are
        # opaque strings. Keep the reversible chunk ID in the small payload.
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"museum-multimodal-lab/chunk/{chunk_id}"))

    @staticmethod
    def _vector(value: Any, dimension: int) -> list[float]:
        if not isinstance(value, list) or len(value) != dimension or not value:
            raise ValueError("embedding vector has an incompatible dimension")
        if any(isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item) for item in value):
            raise ValueError("embedding vector contains a non-finite or non-numeric value")
        if not any(item != 0 for item in value):
            raise ValueError("embedding vector must not be zero for cosine search")
        return [float(item) for item in value]

    def reconcile(
        self,
        rows: list[dict[str, Any]],
        model: str,
        dimension: int,
        *,
        space: str = "text",
    ) -> dict[str, tuple[str, dict[str, str | int], list[float]]]:
        """Make one collection match the authoritative current SQLite rows.

        This can be called after a deletion with ``rows=[]`` to remove old
        points. Any failure propagates; a later call can retry reconciliation.
        """
        if not isinstance(dimension, int) or dimension <= 0:
            raise ValueError("embedding dimension must be positive")
        collection = self._collection_name(model, dimension, space)

        expected: dict[str, tuple[str, dict[str, str | int], list[float]]] = {}
        for row in rows:
            chunk_id = row.get("id")
            version = row.get("index_version")
            document_id = row.get("document_id")
            unit_id = row.get("unit_id")
            document_version = row.get("version")
            if not isinstance(chunk_id, str) or not chunk_id or not isinstance(version, str) or not version:
                raise ValueError("chunk ID and index version are required")
            if (
                not isinstance(document_id, str) or not document_id
                or not isinstance(unit_id, str) or not unit_id
                or isinstance(document_version, bool)
                or not isinstance(document_version, int)
                or document_version <= 0
            ):
                raise ValueError("chunk document, unit, and document version are required")
            if row.get("embedding_model") != model or row.get("embedding_dim") != dimension:
                raise ValueError("chunk embedding model or dimension does not match the query")
            try:
                row_vector = self._vector(json.loads(row["embedding_json"]), dimension)
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise ValueError("chunk embedding is invalid") from exc
            point_id = self._point_id(chunk_id)
            if point_id in expected:
                raise ValueError("duplicate chunk ID in current SQLite snapshot")
            digest = hashlib.sha256(json.dumps(row_vector, separators=(",", ":")).encode("ascii")).hexdigest()
            payload = {
                "chunk_id": chunk_id,
                "document_id": document_id,
                "unit_id": unit_id,
                "document_version": document_version,
                "index_version": version,
                "embedding_sha256": digest,
            }
            if space == "visual":
                page = row.get("page_number") or 0
                if type(page) is not int or page < 0:
                    raise ValueError("visual point has an invalid page")
                payload["page_number"] = page
            expected[point_id] = (chunk_id, payload, row_vector)

        with self._lock:
            if self._closed:
                raise RuntimeError("Qdrant index is closed")
            if not self._client.collection_exists(collection):
                if not expected:
                    return expected
                self._client.create_collection(
                    collection_name=collection,
                    vectors_config=models.VectorParams(size=dimension, distance=models.Distance.COSINE),
                )
            else:
                params = self._client.get_collection(collection).config.params.vectors
                if not isinstance(params, models.VectorParams) or params.size != dimension or params.distance != models.Distance.COSINE:
                    raise RuntimeError("Qdrant collection has incompatible vector settings")

            found: dict[str, dict[str, Any] | None] = {}
            offset = None
            while True:
                page, offset = self._client.scroll(
                    collection_name=collection,
                    offset=offset,
                    limit=256,
                    with_payload=True,
                    with_vectors=False,
                )
                found.update((str(point.id), point.payload) for point in page)
                if offset is None:
                    break

            changed = [
                models.PointStruct(id=point_id, vector=row_vector, payload=payload)
                for point_id, (_, payload, row_vector) in expected.items()
                if found.get(point_id) != payload
            ]
            stale = [point_id for point_id in found if point_id not in expected]
            if changed:
                self._client.upsert(collection_name=collection, points=changed, wait=True)
            if stale:
                self._client.delete(collection_name=collection, points_selector=stale, wait=True)
            return expected

    def query(
        self,
        query_vector: list[float],
        rows: list[dict[str, Any]],
        model: str,
        limit: int = 20,
        *,
        space: str = "text",
    ) -> list[tuple[str, float]]:
        """Reconcile a current SQLite snapshot, then return dense matches.

        A failed Qdrant write or read raises instead of silently returning an
        incomplete search. The service still rechecks SQLite before citations.
        """
        dimension = len(query_vector)
        vector = self._vector(query_vector, dimension)
        if not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        collection = self._collection_name(model, dimension, space)
        with self._lock:
            expected = self.reconcile(rows, model, dimension, space=space)
            if not expected or limit == 0:
                return []

            response = self._client.query_points(
                collection_name=collection,
                query=vector,
                limit=min(limit, len(expected)),
                with_payload=True,
                with_vectors=False,
            )
            result: list[tuple[str, float]] = []
            for point in response.points:
                current = expected.get(str(point.id))
                if current is None or point.payload != current[1]:
                    raise RuntimeError("Qdrant returned a stale or unexpected point")
                result.append((current[0], float(point.score)))
            return result

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._client.close()
                self._closed = True

    def clear_when_empty(self, *, space: str = "text") -> None:
        """Remove this app's derived collections when SQLite has no current chunks."""
        if space not in {"text", "visual"}:
            raise ValueError("unsupported vector space")
        with self._lock:
            if self._closed:
                raise RuntimeError("Qdrant index is closed")
            for collection in self._client.get_collections().collections:
                if collection.name.startswith(f"mml_{space}_"):
                    self._client.delete_collection(collection.name)
