# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import io
import os
import sqlite3
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pymupdf as fitz
import pytest
from conftest import FakeAsr, FakeModels, png_bytes, wait_for_job, wav_bytes
from fastapi.testclient import TestClient
from museum_multimodal_lab.api import create_app
from museum_multimodal_lab.config import Settings, validate_ollama_url
from museum_multimodal_lab.db import Database
from museum_multimodal_lab.extraction import ExtractionError
from museum_multimodal_lab.retrieval import chunk_text
from museum_multimodal_lab.service import (
    ConflictError,
    LabService,
    NotFoundError,
    utc_now,
)
from PIL import Image

QUESTION = "架空展示 GX-418 の観測値は何ですか？"


def other_png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (180, 90), "lightblue").save(output, format="PNG")
    return output.getvalue()


def _extract(service: LabService, document_id: str) -> dict:
    job = wait_for_job(service, service.extract(document_id)["job_id"])
    assert job["status"] == "completed", job
    assert job["phase"] == "内容の確認待ち"
    assert job["progress"] == 1
    return service.document(document_id)


def _approve(service: LabService, document_id: str) -> dict:
    job = wait_for_job(service, service.approve(document_id)["job_id"])
    assert job["status"] == "completed", job
    assert job["phase"] == "登録完了"
    assert job["progress"] == 1
    return service.document(document_id)


def test_image_extract_human_edit_approve_ask_and_restart(
    service: LabService, settings: Settings, fake_models: FakeModels, fake_asr: FakeAsr
) -> None:
    document = service.upload("架空の図表.png", png_bytes())
    assert document["status"] == "draft"
    assert document["version"] == 1
    assert service.ask(QUESTION)["status"] == "held"
    extracted = _extract(service, document["id"])
    assert extracted["status"] == "draft"
    assert len(extracted["units"]) == 1
    unit = extracted["units"][0]
    assert unit["kind"] == "image_interpretation"
    assert unit["extracted_text"] == "架空展示 GX-418 の観測値は 13 単位です。"
    assert unit["corrected_text"] is None
    assert service.ask(QUESTION)["status"] == "held"
    assert fake_models.embed_calls == []  # No embeddings before human approval.

    changed = service.edit_unit(unit["id"], "架空展示 GX-418 の観測値は 31 単位です。")
    assert changed["extracted_text"] == unit["extracted_text"]  # Preserve model draft.
    assert changed["corrected_text"] == "架空展示 GX-418 の観測値は 31 単位です。"
    indexed = _approve(service, document["id"])
    assert indexed["status"] == "indexed"
    assert indexed["units"][0]["status"] == "reviewed"
    result = service.ask(QUESTION)
    assert result["status"] == "answered"
    assert "31" in result["answer"]
    assert "13" not in result["answer"]
    assert len(result["sources"]) == 1
    source = result["sources"][0]
    assert source["document_id"] == document["id"]
    assert source["unit_id"] == unit["id"]
    assert source["kind"] == "image_interpretation"
    assert source["document_name"] == "架空の図表.png"
    assert "31" in source["snippet"] and "13" not in source["snippet"]
    assert source["original_url"] == f"/api/documents/{document['id']}/original"
    assert service.original_path(document["id"])[0].is_file()

    service.close()
    reopened = LabService(settings, fake_models, fake_asr)
    try:
        after_restart = reopened.ask(QUESTION)
        assert after_restart["status"] == "answered"
        assert after_restart["sources"][0]["unit_id"] == unit["id"]
    finally:
        reopened.close()


def test_same_name_replacement_invalidates_old_index_and_delete_removes_new_index(
    service: LabService, fake_models: FakeModels
) -> None:
    first = service.upload("sample.png", png_bytes())
    _extract(service, first["id"])
    _approve(service, first["id"])
    assert service.ask(QUESTION)["status"] == "answered"

    fake_models.image_text = "架空展示 GX-418 の観測値は 44 単位です。"
    second = service.upload("sample.png", other_png_bytes())
    assert second["version"] == 2
    assert service.document(first["id"])["status"] == "superseded"
    assert service.ask(QUESTION)["status"] == "held"
    listed = {row["id"]: row["status"] for row in service.list_documents()["documents"]}
    assert listed == {first["id"]: "superseded", second["id"]: "draft"}
    _extract(service, second["id"])
    _approve(service, second["id"])
    answer = service.ask(QUESTION)
    assert answer["status"] == "answered"
    assert answer["sources"][0]["document_id"] == second["id"]
    assert "44" in answer["sources"][0]["snippet"]

    path, _, _ = service.original_path(second["id"])
    service.delete(second["id"])
    assert not path.exists()
    assert service.ask(QUESTION)["status"] == "held"


def test_reediting_indexed_unit_invalidates_search(service: LabService) -> None:
    doc = service.upload("edit.png", png_bytes())
    unit = _extract(service, doc["id"])["units"][0]
    _approve(service, doc["id"])
    assert service.ask(QUESTION)["status"] == "answered"
    service.edit_unit(unit["id"], "架空展示 GX-418 の観測値は 31 単位です。")
    assert service.document(doc["id"])["status"] == "draft"
    assert service.ask(QUESTION)["status"] == "held"


def test_duplicate_upload_keeps_approved_index(service: LabService) -> None:
    first = service.upload("duplicate.png", png_bytes())
    _extract(service, first["id"])
    _approve(service, first["id"])
    with service.db.connect() as connection:
        before = [row[0] for row in connection.execute("SELECT id FROM chunks WHERE document_id=?", (first["id"],))]
    duplicate = service.upload("duplicate.png", png_bytes())
    assert duplicate["id"] == first["id"]
    assert duplicate["status"] == "indexed"
    assert duplicate["version"] == 1
    with service.db.connect() as connection:
        after = [row[0] for row in connection.execute("SELECT id FROM chunks WHERE document_id=?", (first["id"],))]
    assert after == before
    assert service.ask(QUESTION)["status"] == "answered"


def test_review_history_retains_original_and_each_edit(service: LabService) -> None:
    doc = service.upload("review.png", png_bytes())
    unit = _extract(service, doc["id"])["units"][0]
    service.edit_unit(unit["id"], "架空展示 GX-418 の観測値は 30 単位です。")
    service.edit_unit(unit["id"], "架空展示 GX-418 の観測値は 31 単位です。")
    _approve(service, doc["id"])
    detail = service.document(doc["id"])
    assert detail["units"][0]["extracted_text"] == "架空展示 GX-418 の観測値は 13 単位です。"
    assert detail["units"][0]["corrected_text"] == "架空展示 GX-418 の観測値は 31 単位です。"
    assert [event["action"] for event in detail["review_history"]] == ["edited", "edited", "approved"]
    assert detail["review_history"][0]["previous_text"].endswith("13 単位です。")
    assert detail["review_history"][1]["previous_text"].endswith("30 単位です。")
    assert detail["review_history"][1]["new_text"].endswith("31 単位です。")


def test_model_change_requires_explicit_reindex(settings: Settings, fake_models: FakeModels, fake_asr: FakeAsr) -> None:
    first_service = LabService(settings, fake_models, fake_asr)
    try:
        doc = first_service.upload("model-change.png", png_bytes())
        _extract(first_service, doc["id"])
        _approve(first_service, doc["id"])
        assert first_service.ask(QUESTION)["status"] == "answered"
    finally:
        first_service.close()

    changed_settings = replace(settings, embedding_model="new-local-model")
    changed_service = LabService(changed_settings, fake_models, fake_asr)
    try:
        assert changed_service.document(doc["id"])["reindex_required"] is True
        held = changed_service.ask(QUESTION)
        assert held["status"] == "held"
        assert "再登録" in held["answer"]
        job = wait_for_job(changed_service, changed_service.reindex(doc["id"])["job_id"])
        assert job["status"] == "completed"
        assert changed_service.document(doc["id"])["reindex_required"] is False
        assert changed_service.ask(QUESTION)["status"] == "answered"
    finally:
        changed_service.close()


def test_vector_dimension_change_is_held_and_reindex_recovers(service: LabService, fake_models: FakeModels) -> None:
    doc = service.upload("dimension.png", png_bytes())
    _extract(service, doc["id"])
    _approve(service, doc["id"])
    original_embed = fake_models.embed

    def wider_embed(text: str) -> list[float]:
        return original_embed(text) + [0.5]

    fake_models.embed = wider_embed  # type: ignore[method-assign]
    assert "再登録" in service.ask(QUESTION, "legacy")["answer"]
    assert service.ask(QUESTION)["status"] == "answered"  # The BM25 default does not query with a vector.
    assert wait_for_job(service, service.reindex(doc["id"])["job_id"])["status"] == "completed"
    assert service.ask(QUESTION, "legacy")["status"] == "answered"


def test_reindex_failure_preserves_previous_index_and_can_retry(service: LabService, fake_models: FakeModels) -> None:
    doc = service.upload("reindex-failure.png", png_bytes())
    _extract(service, doc["id"])
    _approve(service, doc["id"])
    with service.db.connect() as connection:
        old_ids = [row[0] for row in connection.execute("SELECT id FROM chunks WHERE document_id=?", (doc["id"],))]
    fake_models.fail_embedding = True
    job = wait_for_job(service, service.reindex(doc["id"])["job_id"])
    assert job["status"] == "failed"
    assert service.document(doc["id"])["status"] == "indexed"
    with service.db.connect() as connection:
        assert [row[0] for row in connection.execute("SELECT id FROM chunks WHERE document_id=?", (doc["id"],))] == old_ids
    fake_models.fail_embedding = False
    assert service.ask(QUESTION)["status"] == "answered"
    assert wait_for_job(service, service.reindex(doc["id"])["job_id"])["status"] == "completed"
    assert service.ask(QUESTION)["status"] == "answered"


def test_reextract_failure_preserves_reviewed_units_and_index(service: LabService, fake_models: FakeModels) -> None:
    doc = service.upload("reextract-failure.png", png_bytes())
    unit = _extract(service, doc["id"])["units"][0]
    service.edit_unit(unit["id"], "架空展示 GX-418 の観測値は 31 単位です。")
    _approve(service, doc["id"])
    with service.db.connect() as connection:
        old_ids = [row[0] for row in connection.execute("SELECT id FROM chunks WHERE document_id=?", (doc["id"],))]
    fake_models.fail_images = True
    failed = wait_for_job(service, service.extract(doc["id"])["job_id"])
    assert failed["status"] == "failed"
    detail = service.document(doc["id"])
    assert detail["status"] == "indexed"
    assert detail["units"][0]["id"] == unit["id"]
    assert detail["units"][0]["corrected_text"].endswith("31 単位です。")
    assert "画像モデル" in detail["error"]
    with service.db.connect() as connection:
        assert [row[0] for row in connection.execute("SELECT id FROM chunks WHERE document_id=?", (doc["id"],))] == old_ids
    assert "31" in service.ask(QUESTION)["answer"]


def test_reextract_success_atomically_replaces_old_index(service: LabService, fake_models: FakeModels) -> None:
    doc = service.upload("reextract-success.png", png_bytes())
    old_unit = _extract(service, doc["id"])["units"][0]
    _approve(service, doc["id"])
    fake_models.image_text = "架空展示 GX-418 の観測値は 44 単位です。"
    refreshed = _extract(service, doc["id"])
    assert refreshed["status"] == "draft"
    assert refreshed["units"][0]["id"] != old_unit["id"]
    assert "44" in refreshed["units"][0]["extracted_text"]
    assert service.ask(QUESTION)["status"] == "held"
    _approve(service, doc["id"])
    assert "44" in service.ask(QUESTION)["answer"]


def test_restart_marks_interrupted_job_and_keeps_prior_index(
    service: LabService, settings: Settings, fake_models: FakeModels, fake_asr: FakeAsr
) -> None:
    doc = service.upload("interrupted.png", png_bytes())
    _extract(service, doc["id"])
    _approve(service, doc["id"])
    now = utc_now()
    with service.db.connect() as connection:
        connection.execute(
            "INSERT INTO jobs(id,document_id,kind,status,progress,started_at,created_at,updated_at) "
            "VALUES(?,?,?,'running',0,?,?,?)",
            ("interrupted-test", doc["id"], "extract", now, now, now),
        )
    service.close()
    restarted = LabService(settings, fake_models, fake_asr)
    try:
        job = restarted.job("interrupted-test")
        assert job["status"] == "failed"
        assert "再起動" in job["error"]
        assert job["duration_sec"] >= 0
        detail = restarted.document(doc["id"])
        assert detail["status"] == "indexed"
        assert "再起動" in detail["error"]
        assert restarted.ask(QUESTION)["status"] == "answered"
    finally:
        restarted.close()


def test_missing_original_cannot_be_cited(service: LabService) -> None:
    doc = service.upload("missing.png", png_bytes())
    _extract(service, doc["id"])
    _approve(service, doc["id"])
    original, _, _ = service.original_path(doc["id"])
    original.unlink()
    assert service.document(doc["id"])["original_available"] is False
    assert service.ask(QUESTION)["status"] == "held"
    with pytest.raises(NotFoundError):
        service.original_path(doc["id"])
    with pytest.raises(ConflictError):
        service.reindex(doc["id"])


def test_changed_original_cannot_be_cited_or_reindexed(service: LabService) -> None:
    doc = service.upload("tampered.png", png_bytes())
    _extract(service, doc["id"])
    _approve(service, doc["id"])
    original, _, _ = service.original_path(doc["id"])
    original.write_bytes(original.read_bytes() + b"tampered outside the app")
    assert service.document(doc["id"])["original_available"] is False
    assert service.ask(QUESTION)["status"] == "held"
    with pytest.raises(ConflictError, match="変更"):
        service.original_path(doc["id"])
    with pytest.raises(ConflictError):
        service.reindex(doc["id"])


def test_delete_failure_is_visible_retryable_and_invalidates_index(service: LabService, monkeypatch: pytest.MonkeyPatch) -> None:
    doc = service.upload("delete-failure.png", png_bytes())
    _extract(service, doc["id"])
    _approve(service, doc["id"])
    original, _, _ = service.original_path(doc["id"])
    actual_unlink = Path.unlink

    def fail_target(path: Path, *args, **kwargs):
        if path == original:
            raise PermissionError("in use")
        return actual_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr(Path, "unlink", fail_target)
        with pytest.raises(ConflictError, match="再試行"):
            service.delete(doc["id"])
    detail = service.document(doc["id"])
    assert detail["status"] == "superseded"
    assert "再試行" in detail["error"]
    assert original.is_file()
    assert service.ask(QUESTION)["status"] == "held"
    service.delete(doc["id"])
    assert not original.exists()
    with pytest.raises(NotFoundError):
        service.document(doc["id"])


def test_configured_local_frontend_origin_only(settings: Settings, fake_models: FakeModels, fake_asr: FakeAsr) -> None:
    app = create_app(replace(settings, frontend_port=5175), fake_models, fake_asr)
    with TestClient(app) as client:
        assert client.post("/api/ask", json={"question": "確認"}, headers={"origin": "http://127.0.0.1:5175"}).status_code == 200
        assert client.post("/api/ask", json={"question": "確認"}, headers={"origin": "http://127.0.0.1:5174"}).status_code == 403


def test_api_reindex_route(settings: Settings, fake_models: FakeModels, fake_asr: FakeAsr) -> None:
    app = create_app(settings, fake_models, fake_asr)
    with TestClient(app) as client:
        document = client.post("/api/documents", files={"file": ("api-reindex.png", png_bytes(), "image/png")}).json()
        extracted = client.post(f"/api/documents/{document['id']}/extract")
        assert wait_for_job(app.state.service, extracted.json()["job_id"])["status"] == "completed"
        approved = client.post(f"/api/documents/{document['id']}/approve")
        assert wait_for_job(app.state.service, approved.json()["job_id"])["status"] == "completed"
        reindexed = client.post(f"/api/documents/{document['id']}/reindex")
        assert reindexed.status_code == 202
        assert wait_for_job(app.state.service, reindexed.json()["job_id"])["status"] == "completed"
        assert client.get(f"/api/documents/{document['id']}").json()["status"] == "indexed"


def test_additive_chunk_migration_backfills_dimensions(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE chunks (id TEXT PRIMARY KEY, document_id TEXT NOT NULL, unit_id TEXT NOT NULL, "
            "search_text TEXT NOT NULL, citation_text TEXT NOT NULL, embedding_json TEXT NOT NULL, "
            "embedding_model TEXT NOT NULL, index_version TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO chunks VALUES ('old','doc','unit','text','text','[1.0, 2.0]','local','v1','2026-01-01')"
        )
    Database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT embedding_dim, ordinal FROM chunks WHERE id='old'").fetchone() == (2, 0)
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='review_events'").fetchone()[0] == "review_events"


def test_importing_api_does_not_open_default_data_dir(tmp_path: Path) -> None:
    data_dir = tmp_path / "not-opened"
    environment = os.environ.copy()
    environment["MML_DATA_DIR"] = str(data_dir)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "backend")
    result = subprocess.run(
        [sys.executable, "-c", "import museum_multimodal_lab.api"],
        # Functional no-write test: a cold Windows import can be delayed by paging.
        check=False, capture_output=True, text=True, env=environment, timeout=45,
    )
    assert result.returncode == 0, result.stderr
    assert not data_dir.exists()


def test_pdf_text_page_provenance(service: LabService, fake_models: FakeModels) -> None:
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "TX-999 sample value 8.4", fontsize=14)
    data = pdf.tobytes()
    pdf.close()
    doc = service.upload("架空レポート.pdf", data)
    extracted = _extract(service, doc["id"])
    assert len(extracted["units"]) == 1
    unit = extracted["units"][0]
    assert unit["kind"] == "pdf_text"
    assert unit["page_number"] == 1
    assert "TX-999" in unit["extracted_text"]
    assert fake_models.image_calls == 0
    _approve(service, doc["id"])
    answer = service.ask("TX-999 の値は？")
    assert answer["status"] == "answered"
    assert answer["sources"][0]["page_number"] == 1
    assert answer["sources"][0]["original_url"].endswith("#page=1")


def test_long_pdf_chunks_have_stable_order_and_model_dimension(service: LabService) -> None:
    pdf = fitz.open()
    pdf.new_page().insert_text((72, 72), "TX-999 initial text", fontsize=14)
    document = service.upload("ordered.pdf", pdf.tobytes())
    pdf.close()
    unit = _extract(service, document["id"])["units"][0]
    long_text = "TX-999 heading\n\n" + "\n\n".join(f"段落 {index}: " + "あ" * 55 for index in range(100))
    service.edit_unit(unit["id"], long_text)
    _approve(service, document["id"])
    expected = chunk_text(long_text, "pdf_text")
    assert len(expected) > 1
    with service.db.connect() as connection:
        rows = connection.execute(
            "SELECT ordinal, embedding_dim, search_text FROM chunks WHERE document_id=? ORDER BY ordinal",
            (document["id"],),
        ).fetchall()
    assert [row["ordinal"] for row in rows] == list(range(len(expected)))
    assert [row["embedding_dim"] for row in rows] == [3] * len(expected)
    assert [row["search_text"] for row in rows] == expected


def test_image_only_pdf_is_not_silently_registered_as_empty(
    service: LabService, fake_models: FakeModels
) -> None:
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_image(fitz.Rect(24, 24, 200, 120), stream=png_bytes())
    data = pdf.tobytes()
    pdf.close()
    document = service.upload("画像だけのPDF.pdf", data)
    extracted = _extract(service, document["id"])
    assert fake_models.image_calls == 1
    assert len(extracted["units"]) == 1
    assert extracted["units"][0]["kind"] == "image_interpretation"
    assert extracted["units"][0]["page_number"] == 1
    assert service.ask(QUESTION)["status"] == "held"
    _approve(service, document["id"])
    answer = service.ask(QUESTION)
    assert answer["status"] == "answered"
    assert answer["sources"][0]["original_url"].endswith("#page=1")


def test_audio_segments_keep_times_and_original_seek(service: LabService, fake_asr: FakeAsr) -> None:
    doc = service.upload("収録サンプル.wav", wav_bytes())
    extracted = _extract(service, doc["id"])
    assert fake_asr.calls == 1
    assert extracted["units"][0]["kind"] == "audio_transcript"
    assert extracted["units"][0]["start_sec"] == pytest.approx(0.1)
    assert extracted["units"][0]["end_sec"] == pytest.approx(0.7)
    _approve(service, doc["id"])
    result = service.ask("GX-418 の値は？")
    assert result["status"] == "answered"
    assert result["sources"][0]["kind"] == "audio_transcript"
    assert result["sources"][0]["original_url"].endswith("#t=0.10")


def test_audio_out_of_bounds_fails_without_units(service: LabService, fake_asr: FakeAsr) -> None:
    fake_asr.segments = [{"start_sec": 0.2, "end_sec": 4.0, "text": "GX-418"}]
    doc = service.upload("invalid-time.wav", wav_bytes())
    job = wait_for_job(service, service.extract(doc["id"])["job_id"])
    assert job["status"] == "failed"
    assert service.document(doc["id"])["status"] == "failed"
    assert service.document(doc["id"])["units"] == []


def test_forged_citation_and_number_are_not_returned(
    service: LabService, fake_models: FakeModels
) -> None:
    doc = service.upload("guard.png", png_bytes())
    _extract(service, doc["id"])
    _approve(service, doc["id"])
    fake_models.forged_citation = True
    assert service.ask(QUESTION)["status"] == "held"
    fake_models.forged_citation = False
    fake_models.unverified_number = True
    assert service.ask(QUESTION)["status"] == "held"
    fake_models.unverified_number = False
    assert service.ask("架空展示 GX-999 の観測値は？")["status"] == "held"


@pytest.mark.parametrize("change", ["delete", "replace", "edit"])
def test_answer_is_held_if_cited_evidence_changes_during_generation(
    service: LabService, fake_models: FakeModels, change: str
) -> None:
    document = service.upload("race.png", png_bytes())
    unit = _extract(service, document["id"])["units"][0]
    _approve(service, document["id"])
    original_answer = fake_models.answer

    def answer_then_change(question: str, evidence: list[dict[str, str]]) -> dict:
        proposed = original_answer(question, evidence)
        if change == "delete":
            service.delete(document["id"])
        elif change == "replace":
            service.upload("race.png", other_png_bytes())
        else:
            service.edit_unit(unit["id"], "架空展示 GX-418 の観測値は 31 単位です。")
        return proposed

    fake_models.answer = answer_then_change  # type: ignore[method-assign]
    response = service.ask(QUESTION)
    assert response == {"status": "held", "answer": "登録資料からは確認できません。", "sources": []}


def test_model_failure_does_not_create_partial_index(service: LabService, fake_models: FakeModels) -> None:
    doc = service.upload("failure.png", png_bytes())
    fake_models.fail_images = True
    failed_extract = wait_for_job(service, service.extract(doc["id"])["job_id"])
    assert failed_extract["status"] == "failed"
    assert service.document(doc["id"])["units"] == []
    fake_models.fail_images = False
    _extract(service, doc["id"])
    fake_models.fail_embedding = True
    failed_approval = wait_for_job(service, service.approve(doc["id"])["job_id"])
    assert failed_approval["status"] == "failed"
    assert service.document(doc["id"])["status"] == "reviewed"
    assert service.ask(QUESTION)["status"] == "held"


def test_same_name_upload_is_blocked_while_old_extraction_runs(
    settings: Settings, fake_asr: FakeAsr
) -> None:
    started = threading.Event()
    release = threading.Event()

    class SlowModels(FakeModels):
        def inspect_image(self, image: bytes) -> str:
            started.set()
            assert release.wait(5), "test worker was not released"
            return super().inspect_image(image)

    service = LabService(settings, SlowModels(), fake_asr)
    running: str | None = None
    try:
        original = service.upload("busy.png", png_bytes())
        running = service.extract(original["id"])["job_id"]
        assert started.wait(5), "model did not start"
        with pytest.raises(ConflictError):
            service.upload("busy.png", other_png_bytes())
    finally:
        release.set()
        if running is not None:
            assert wait_for_job(service, running)["status"] == "completed"
        service.close()
    assert service.document(original["id"])["status"] == "draft"


@pytest.mark.parametrize(
    "url",
    ["https://127.0.0.1:11434", "http://localhost:11434", "http://192.168.1.2:11434", "http://evil.test:11434", "http://127.0.0.1:11434/path", "http://127.0.0.1:11434@evil.test:80"],
)
def test_ollama_external_or_ambiguous_destination_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        validate_ollama_url(url)


def test_corrupt_oversized_and_path_names_rejected(service: LabService, settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(ExtractionError):
        service.upload("../escape.png", png_bytes())
    with pytest.raises(ExtractionError):
        service.upload("broken.png", b"not a PNG")
    with pytest.raises(ExtractionError):
        service.upload("bad.pdf", b"%PDF-not-a-document")
    tiny_limit = Settings(data_dir=tmp_path / "smaller", ollama_url="http://127.0.0.1:11434", max_upload_bytes=10)
    small_service = LabService(tiny_limit, FakeModels(), FakeAsr())
    try:
        with pytest.raises(ExtractionError):
            small_service.upload("large.png", png_bytes())
    finally:
        small_service.close()
    assert service.list_documents()["documents"] == []
