# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

"""Visual retrieval integration with synthetic documents and injected models."""

from __future__ import annotations

from dataclasses import replace

import pymupdf
import pytest
from conftest import FakeAsr, FakeModels, png_bytes, wait_for_job
from fastapi.testclient import TestClient
from museum_multimodal_lab.api import create_app
from museum_multimodal_lab.config import Settings
from museum_multimodal_lab.providers import ModelError
from museum_multimodal_lab.service import LabService
from museum_multimodal_lab.visual_retrieval import visual_pages, visual_rank


class FakeVisual:
    model_id = "fake-visual-v1"
    dimension = 2

    def __init__(self):
        self.pages = []
        self.fail = False

    def encode_image(self, image):
        if self.fail:
            raise ValueError("injected ONNX failure")
        self.pages.append(image.size)
        return [1.0, 0.0]

    def encode_text(self, text):
        return [0.0, 1.0] if text == "unrelated" else [1.0, 0.0]


def finish(lab: LabService, job: dict) -> None:
    assert wait_for_job(lab, job["job_id"])["status"] == "completed"


def test_visual_persists_and_uses_current_approved_image_only(settings, fake_models, fake_asr):
    visual = FakeVisual()
    lab = LabService(settings, fake_models, fake_asr, visual=visual)
    try:
        image_id = lab.upload("demo.png", png_bytes())["id"]
        finish(lab, lab.extract(image_id))
        assert lab.ask("GX-418", "visual")["status"] == "held"
        assert not visual.pages
        finish(lab, lab.approve(image_id))
        assert visual.pages == [(180, 90)]
        assert lab._qdrant._client.get_collections().collections[0].name.startswith("mml_visual_")
        match = lab.ask("GX-418", "visual")
        assert match["status"] == "answered"
        assert match["sources"][0]["retrieval_origin"] == "visual"
        assert match["sources"][0]["document_id"] == image_id
        assert lab.ask("GX-4180", "visual")["status"] == "held"
        assert lab.ask("unrelated", "visual")["status"] == "held"
        assert lab.ask("GX-418", "multimodal")["status"] == "answered"
        assert {item.name.split("_")[1] for item in lab._qdrant._client.get_collections().collections} == {"text", "visual"}
    finally:
        lab.close()
    reopened = LabService(settings, fake_models, fake_asr, visual=visual)
    try:
        assert reopened.ask("GX-418", "visual")["status"] == "answered"
        assert len(visual.pages) == 1
        unit_id = reopened.document(image_id)["units"][0]["id"]
        reopened.edit_unit(unit_id, "GX-418 の観測値は 31 単位です。")
        assert reopened.ask("GX-418", "visual")["status"] == "held"
        assert all(not item.name.startswith("mml_visual_") for item in reopened._qdrant._client.get_collections().collections)
        finish(reopened, reopened.approve(image_id))
        assert "31" in reopened.ask("GX-418", "visual")["answer"]
        reopened.delete(image_id)
        assert reopened.ask("GX-418", "visual")["status"] == "held"
    finally:
        reopened.close()


def test_pdf_page_mapping_and_model_change(settings, fake_models, fake_asr):
    fake_models.answer = lambda question, evidence: {"answer": evidence[0]["text"], "source_ids": [evidence[0]["id"]]}
    encoder = FakeVisual()
    lab = LabService(settings, fake_models, fake_asr, visual=encoder)
    try:
        with pymupdf.open() as pdf:
            for text in ("AB-892 temperature 13", "CD-232 temperature 31"):
                pdf.new_page().insert_text((40, 48), text)
            doc_id = lab.upload("fictional.pdf", pdf.tobytes())["id"]
        finish(lab, lab.extract(doc_id))
        finish(lab, lab.approve(doc_id))
        assert len(encoder.pages) == 2
        result = lab.ask("CD-232 temperature", "visual")
        assert result["sources"][0]["page_number"] == 2
        assert result["sources"][0]["original_url"].endswith("#page=2")
        encoder.model_id = "fake-visual-new"
        assert lab.health()["visual_retrieval"] == "needs_reindex"
        assert "visual" not in lab.health()["retrieval_modes"]
        with pytest.raises(ModelError, match="視覚索引がない"):
            lab.ask("CD-232 temperature", "multimodal")
        assert lab.ask("CD-232 temperature", "legacy")["status"] == "answered"
        finish(lab, lab.reindex(doc_id))
        assert lab.health()["visual_retrieval"] == "ready"
        assert lab.ask("CD-232 temperature", "visual")["sources"][0]["page_number"] == 2
    finally:
        lab.close()


def test_visual_number_must_match_complete_signed_token(settings, fake_models, fake_asr):
    fake_models.image_text = "GX-418 の記録は 142 単位、最低 -42 単位です。"
    fake_models.answer = lambda question, evidence: {"answer": "42 単位です。", "source_ids": ["S1"]}
    lab = LabService(settings, fake_models, fake_asr, visual=FakeVisual())
    try:
        doc_id = lab.upload("demo.png", png_bytes())["id"]
        finish(lab, lab.extract(doc_id))
        finish(lab, lab.approve(doc_id))
        assert lab.ask("GX-418", "visual")["status"] == "held"
    finally:
        lab.close()


def test_vlm_selects_approved_excerpt_without_exposing_its_draft(settings, fake_models, fake_asr):
    fake_models.image_text = "aquarium 999999"
    lab = LabService(replace(settings, visual_assist=True), fake_models, fake_asr, visual=FakeVisual())
    try:
        with pymupdf.open() as pdf:
            page = pdf.new_page(height=1500)
            for index in range(48):
                page.insert_text((40, 40 + index * 20), "fictional record of ordinary workshop operations")
            page.insert_text((40, 1030), "aquarium blue")
            doc_id = lab.upload("fictional.pdf", pdf.tobytes())["id"]
        finish(lab, lab.extract(doc_id))
        finish(lab, lab.approve(doc_id))
        result = lab.ask("color", "visual")
        assert result["retrieval_trace"] == [{"action": "visual_assist", "reason": "approved_excerpt_selected"}]
        assert fake_models.image_calls == 1
        evidence = fake_models.answer_calls[-1][1]
        assert "aquarium blue" in evidence[0]["text"]
        assert "999999" not in str(evidence)
    finally:
        lab.close()


def test_visual_failure_does_not_block_text_approval(settings, fake_models, fake_asr):
    visual = FakeVisual()
    visual.fail = True
    lab = LabService(settings, fake_models, fake_asr, visual=visual)
    try:
        doc_id = lab.upload("demo.png", png_bytes())["id"]
        finish(lab, lab.extract(doc_id))
        finish(lab, lab.approve(doc_id))
        assert lab.document(doc_id)["status"] == "indexed"
        assert lab.ask("GX-418", "bm25")["status"] == "answered"
        with pytest.raises(ModelError, match="視覚索引がない"):
            lab.ask("GX-418", "visual")
        visual.fail = False
        finish(lab, lab.reindex(doc_id))
        assert lab.ask("GX-418", "visual")["status"] == "answered"
    finally:
        lab.close()


def test_api_hides_unconfigured_visual_and_rejects_direct_use(settings: Settings, fake_models: FakeModels, fake_asr: FakeAsr):
    with TestClient(create_app(settings, fake_models, fake_asr)) as client:
        assert "visual" not in client.get("/api/health").json()["retrieval_modes"]
        assert client.post("/api/ask", json={"question": "test", "retrieval_mode": "visual"}).status_code == 503


def test_weak_visual_similarity_requires_exact_identifier():
    row = {"id": "chunk", "document_id": "doc", "unit_id": "unit", "page_number": 1,
           "mime_type": "application/pdf", "search_text": "LM-482 水温 17"}
    pages = visual_pages([row])
    assert visual_rank("クジラの絵", pages, [("chunk", 0.267)]) == []
    assert visual_rank("LM-482 水温", pages, [("chunk", 0.264)])[0]["id"] == "chunk"
    assert visual_rank("LM-4820 水温", pages, [("chunk", 0.264)]) == []
