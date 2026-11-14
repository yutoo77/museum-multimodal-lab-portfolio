# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import pymupdf as fitz
from conftest import FakeAsr, FakeModels, png_bytes, wait_for_job, wav_bytes
from fastapi.testclient import TestClient
from museum_multimodal_lab.api import create_app
from museum_multimodal_lab.config import Settings


def test_mock_api_image_full_flow_and_original(settings: Settings, fake_models: FakeModels, fake_asr: FakeAsr) -> None:
    app = create_app(settings, fake_models, fake_asr)
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["ollama"]["available"] is True
        assert health.json()["documents_count"] == 0

        uploaded = client.post("/api/documents", files={"file": ("mock-image.png", png_bytes(), "image/png")})
        assert uploaded.status_code == 201
        document_id = uploaded.json()["id"]
        assert uploaded.json()["status"] == "draft"
        assert uploaded.json()["mime_type"] == "image/png"
        assert client.post(f"/api/documents/{document_id}/approve").status_code == 409

        job_response = client.post(f"/api/documents/{document_id}/extract")
        assert job_response.status_code == 202
        assert wait_for_job(app.state.service, job_response.json()["job_id"])["status"] == "completed"
        detail = client.get(f"/api/documents/{document_id}").json()
        assert len(detail["units"]) == 1
        assert detail["units"][0]["kind"] == "image_interpretation"
        assert client.post("/api/ask", json={"question": "GX-418 の観測値は？"}).json()["status"] == "held"

        unit_id = detail["units"][0]["id"]
        edited = client.patch(f"/api/extracted-units/{unit_id}", json={"corrected_text": "架空展示 GX-418 の観測値は 31 単位です。"})
        assert edited.status_code == 200
        assert edited.json()["corrected_text"].endswith("31 単位です。")
        approve_response = client.post(f"/api/documents/{document_id}/approve")
        assert approve_response.status_code == 202
        assert wait_for_job(app.state.service, approve_response.json()["job_id"])["status"] == "completed"
        answer = client.post("/api/ask", json={"question": "GX-418 の観測値は？"})
        assert answer.status_code == 200
        assert answer.json()["status"] == "answered"
        assert answer.json()["sources"][0]["document_id"] == document_id
        assert client.get(answer.json()["sources"][0]["original_url"]).content == png_bytes()

        listed = client.get("/api/documents")
        assert listed.status_code == 200
        assert [document["id"] for document in listed.json()["documents"]] == [document_id]
        removed = client.delete(f"/api/documents/{document_id}")
        assert removed.status_code == 204
        assert client.get(f"/api/documents/{document_id}").status_code == 404
        assert client.post("/api/ask", json={"question": "GX-418 の観測値は？"}).json()["status"] == "held"


def test_audio_original_supports_range_request(settings: Settings, fake_models: FakeModels, fake_asr: FakeAsr) -> None:
    app = create_app(settings, fake_models, fake_asr)
    with TestClient(app) as client:
        uploaded = client.post("/api/documents", files={"file": ("short.wav", wav_bytes(), "audio/wav")})
        assert uploaded.status_code == 201
        document_id = uploaded.json()["id"]
        partial = client.get(f"/api/documents/{document_id}/original", headers={"range": "bytes=0-15"})
        assert partial.status_code == 206
        assert partial.headers["content-range"].startswith("bytes 0-15/")
        assert partial.content == wav_bytes()[:16]


def test_pdf_page_preview_is_a_real_local_render(settings: Settings, fake_models: FakeModels, fake_asr: FakeAsr) -> None:
    pdf = fitz.open()
    pdf.new_page().insert_text((72, 72), "local PDF page", fontsize=14)
    data = pdf.tobytes()
    pdf.close()
    app = create_app(settings, fake_models, fake_asr)
    with TestClient(app) as client:
        uploaded = client.post("/api/documents", files={"file": ("local.pdf", data, "application/pdf")})
        assert uploaded.status_code == 201
        document_id = uploaded.json()["id"]
        page = client.get(f"/api/documents/{document_id}/pages/1/preview")
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("image/png")
        assert page.content.startswith(b"\x89PNG\r\n\x1a\n")
        assert client.get(f"/api/documents/{document_id}/pages/2/preview").status_code == 404


def test_large_pdf_page_is_scaled_before_vlm_render(settings: Settings, fake_asr: FakeAsr) -> None:
    class MeasuringModels(FakeModels):
        image_size: tuple[int, int] | None = None

        def inspect_image(self, image: bytes) -> str:
            self.image_size = (int.from_bytes(image[16:20], "big"), int.from_bytes(image[20:24], "big"))
            return super().inspect_image(image)

    pdf = fitz.open()
    pdf.new_page(width=10_000, height=10_000)
    data = pdf.tobytes()
    pdf.close()
    models = MeasuringModels()
    app = create_app(settings, models, fake_asr)
    with TestClient(app) as client:
        uploaded = client.post("/api/documents", files={"file": ("huge-page.pdf", data, "application/pdf")})
        assert uploaded.status_code == 201
        job = client.post(f"/api/documents/{uploaded.json()['id']}/extract")
        assert job.status_code == 202
        assert wait_for_job(app.state.service, job.json()["job_id"])["status"] == "completed"
        assert models.image_size is not None
        assert models.image_size[0] <= 1280
        assert models.image_size[1] <= 1280


def test_api_upload_rejects_bad_content_and_untrusted_host(settings: Settings, fake_models: FakeModels, fake_asr: FakeAsr) -> None:
    app = create_app(settings, fake_models, fake_asr)
    with TestClient(app) as client:
        bad = client.post("/api/documents", files={"file": ("bad.png", b"secret text", "image/png")})
        assert bad.status_code == 400
        assert client.get("/api/health", headers={"host": "external.example"}).status_code == 400
        foreign = client.post(
            "/api/documents",
            files={"file": ("fake.png", png_bytes(), "image/png")},
            headers={"origin": "https://foreign.example"},
        )
        assert foreign.status_code == 403
        assert client.post("/api/ask", json={"question": "確認"}, headers={"sec-fetch-site": "cross-site"}).status_code == 403
        assert client.post("/api/ask", json={"question": ""}).status_code == 422
