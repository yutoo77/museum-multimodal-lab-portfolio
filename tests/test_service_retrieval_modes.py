# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

"""Isolated end-to-end service checks with fake models and real local Qdrant."""

from __future__ import annotations

import pytest
from conftest import FakeAsr, FakeModels, png_bytes, wait_for_job
from museum_multimodal_lab.config import Settings
from museum_multimodal_lab.providers import ModelError
from museum_multimodal_lab.service import LabService

QUESTION = "架空展示 GX-418 の観測値は何ですか？"


def _complete(service: LabService, document_id: str) -> str:
    extracted = wait_for_job(service, service.extract(document_id)["job_id"])
    assert extracted["status"] == "completed"
    unit_id = service.document(document_id)["units"][0]["id"]
    approved = wait_for_job(service, service.approve(document_id)["job_id"])
    assert approved["status"] == "completed"
    return unit_id


def test_four_modes_keep_approved_evidence_and_default_bm25(
    service: LabService, fake_models: FakeModels
) -> None:
    document = service.upload("架空の観測図.png", png_bytes())
    assert service.ask(QUESTION, "hybrid")["status"] == "held"
    unit_id = _complete(service, document["id"])
    for mode in ("legacy", "bm25", "dense", "hybrid"):
        before = len(fake_models.embed_calls)
        result = service.ask(QUESTION, mode)
        assert result["status"] == "answered", mode
        assert result["sources"][0]["document_id"] == document["id"]
        assert result["sources"][0]["unit_id"] == unit_id
        assert "13" in result["sources"][0]["snippet"]
        if mode == "bm25":
            assert len(fake_models.embed_calls) == before  # BM25 does not embed the query.
    before = len(fake_models.embed_calls)
    assert service.ask(QUESTION) == service.ask(QUESTION, "bm25")
    assert len(fake_models.embed_calls) == before


def test_qdrant_modes_do_not_retrieve_old_version_after_edit_or_delete(
    service: LabService, fake_models: FakeModels
) -> None:
    document = service.upload("架空の観測図.png", png_bytes())
    unit_id = _complete(service, document["id"])
    assert service.ask(QUESTION, "hybrid")["status"] == "answered"

    service.edit_unit(unit_id, "架空展示 GX-418 の観測値は 31 単位です。")
    assert service.ask(QUESTION, "dense")["status"] == "held"
    assert service.ask(QUESTION, "hybrid")["status"] == "held"
    approved = wait_for_job(service, service.approve(document["id"])["job_id"])
    assert approved["status"] == "completed"
    for mode in ("dense", "hybrid"):
        result = service.ask(QUESTION, mode)
        assert result["status"] == "answered"
        assert "31" in result["answer"] and "13" not in result["answer"]

    fake_models.unverified_number = True
    assert service.ask(QUESTION, "hybrid")["status"] == "held"
    fake_models.unverified_number = False
    fake_models.forged_citation = True
    assert service.ask(QUESTION, "hybrid")["status"] == "held"
    fake_models.forged_citation = False

    service.delete(document["id"])
    assert service.ask(QUESTION, "dense")["status"] == "held"
    assert service.ask(QUESTION, "hybrid")["status"] == "held"
    assert service._qdrant._client.get_collections().collections == []


def test_local_qdrant_reconciles_after_restart(
    settings: Settings, fake_models: FakeModels, fake_asr: FakeAsr
) -> None:
    first = LabService(settings, fake_models, fake_asr)
    try:
        document = first.upload("架空の観測図.png", png_bytes())
        _complete(first, document["id"])
        assert first.ask(QUESTION, "dense")["status"] == "answered"
    finally:
        first.close()
    second = LabService(settings, fake_models, fake_asr)
    try:
        result = second.ask(QUESTION, "dense")
        assert result["status"] == "answered"
        assert result["sources"][0]["document_id"] == document["id"]
    finally:
        second.close()


def test_qdrant_failure_is_explicit_and_default_and_legacy_remain_available(service: LabService) -> None:
    document = service.upload("架空の観測図.png", png_bytes())
    _complete(service, document["id"])

    class BrokenIndex:
        def query(self, *args, **kwargs):
            raise OSError("test-only local index failure")

        def close(self) -> None:
            pass

    service._qdrant = BrokenIndex()
    with pytest.raises(ModelError, match="ローカルQdrant"):
        service.ask(QUESTION, "dense")
    assert service.ask(QUESTION)["status"] == "answered"
    assert service.ask(QUESTION, "legacy")["status"] == "answered"
