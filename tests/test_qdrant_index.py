# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import json
from pathlib import Path

import pytest
from museum_multimodal_lab.qdrant_index import QdrantIndex
from qdrant_client import QdrantClient


def row(
    chunk_id: str,
    vector: list[float],
    *,
    version: str = "v1",
    model: str = "test-embedding",
) -> dict[str, object]:
    return {
        "id": chunk_id,
        "document_id": "document-1",
        "unit_id": "unit-1",
        "version": 1,
        "search_text": "This source text must not enter Qdrant payload.",
        "citation_text": "Nor may this citation text.",
        "embedding_json": json.dumps(vector),
        "embedding_dim": len(vector),
        "embedding_model": model,
        "index_version": version,
    }


def stored_points(path: Path) -> list[object]:
    client = QdrantClient(path=str(path))
    try:
        collections = client.get_collections().collections
        points = []
        for collection in collections:
            offset = None
            while True:
                page, offset = client.scroll(
                    collection_name=collection.name,
                    offset=offset,
                    limit=10,
                    with_payload=True,
                    with_vectors=False,
                )
                points.extend(page)
                if offset is None:
                    break
        return points
    finally:
        client.close()


def test_query_persists_and_uses_only_metadata_payload(tmp_path: Path) -> None:
    index = QdrantIndex(tmp_path)
    rows = [row("chunk-a", [1.0, 0.0]), row("chunk-b", [0.0, 1.0])]
    try:
        result = index.query([1.0, 0.0], rows, "test-embedding")
        assert [chunk_id for chunk_id, _ in result] == ["chunk-a", "chunk-b"]
        assert result[0][1] > result[1][1]
    finally:
        index.close()

    points = stored_points(tmp_path / "qdrant")
    assert len(points) == 2
    for point in points:
        assert point.payload is not None
        assert set(point.payload) == {
            "chunk_id", "document_id", "unit_id", "document_version", "index_version", "embedding_sha256"
        }
        assert "source" not in str(point.payload).lower()
        assert "citation" not in str(point.payload).lower()

    reopened = QdrantIndex(tmp_path)
    try:
        assert reopened.query([1.0, 0.0], rows, "test-embedding")[0][0] == "chunk-a"
    finally:
        reopened.close()


def test_reconciles_deleted_and_reindexed_chunks(tmp_path: Path) -> None:
    index = QdrantIndex(tmp_path)
    try:
        original = [row("old", [1.0, 0.0]), row("keep", [0.0, 1.0])]
        assert len(index.query([1.0, 0.0], original, "test-embedding")) == 2
        replacement = [row("keep", [1.0, 0.0], version="v2")]
        assert [item[0] for item in index.query([1.0, 0.0], replacement, "test-embedding")] == ["keep"]
        index.reconcile([], "test-embedding", 2)
        collection = index._client.get_collections().collections[0].name
        points, _ = index._client.scroll(collection, with_payload=True)
        assert points == []
        assert index.query([1.0, 0.0], [], "test-embedding") == []
    finally:
        index.close()
    assert stored_points(tmp_path / "qdrant") == []


def test_reconciles_vector_change_even_if_version_is_reused(tmp_path: Path) -> None:
    index = QdrantIndex(tmp_path)
    try:
        index.query([1.0, 0.0], [row("same", [1.0, 0.0])], "test-embedding")
        result = index.query([1.0, 0.0], [row("same", [0.0, 1.0])], "test-embedding")
        assert result[0][0] == "same"
        assert result[0][1] < 0.1
    finally:
        index.close()


def test_model_and_dimension_collections_do_not_mix(tmp_path: Path) -> None:
    index = QdrantIndex(tmp_path)
    try:
        assert index.query([1.0, 0.0], [row("a", [1.0, 0.0])], "test-embedding")[0][0] == "a"
        assert index.query([0.0, 1.0, 0.0], [row("b", [0.0, 1.0, 0.0], model="other")], "other")[0][0] == "b"
        assert len(index._client.get_collections().collections) == 2
    finally:
        index.close()


def test_clear_when_sqlite_has_no_current_chunks_removes_only_app_collections(tmp_path: Path) -> None:
    index = QdrantIndex(tmp_path)
    try:
        index.query([1.0, 0.0], [row("a", [1.0, 0.0])], "test-embedding")
        index.query([0.0, 1.0, 0.0], [row("b", [0.0, 1.0, 0.0], model="other")], "other")
        assert len(index._client.get_collections().collections) == 2
        index.clear_when_empty()
        assert index._client.get_collections().collections == []
    finally:
        index.close()


def test_invalid_vectors_fail_closed(tmp_path: Path) -> None:
    index = QdrantIndex(tmp_path)
    try:
        with pytest.raises(ValueError):
            index.query([1.0, 0.0], [row("bad", [1.0, 0.0], model="other")], "test-embedding")
        with pytest.raises(ValueError):
            index.query([1.0, 0.0], [row("bad", [1.0])], "test-embedding")
        with pytest.raises(ValueError):
            index.query([float("nan"), 0.0], [], "test-embedding")
        with pytest.raises(ValueError):
            index.query([0.0, 0.0], [], "test-embedding")
    finally:
        index.close()


def test_qdrant_write_failure_is_not_hidden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    index = QdrantIndex(tmp_path)
    try:
        def broken_upsert(*args: object, **kwargs: object) -> None:
            raise RuntimeError("simulated Qdrant failure")

        monkeypatch.setattr(index._client, "upsert", broken_upsert)
        with pytest.raises(RuntimeError, match="simulated Qdrant failure"):
            index.query([1.0, 0.0], [row("a", [1.0, 0.0])], "test-embedding")
    finally:
        index.close()
