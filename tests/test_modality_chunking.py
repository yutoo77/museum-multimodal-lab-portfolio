# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

"""Modality-aware chunking through the review and search flow, using fakes only."""

from __future__ import annotations

from typing import Any

import pymupdf as fitz
from conftest import FakeAsr, FakeModels, png_bytes, wait_for_job, wav_bytes
from museum_multimodal_lab.providers import ModelError
from museum_multimodal_lab.retrieval import chunk_text
from museum_multimodal_lab.service import LabService


def _pdf_bytes() -> bytes:
    pdf = fitz.open()
    pdf.new_page().insert_text((72, 72), "Fictional page for reviewed text", fontsize=12)
    data = pdf.tobytes()
    pdf.close()
    return data


def _upload_and_extract(service: LabService, name: str, data: bytes) -> dict[str, Any]:
    document = service.upload(name, data)
    extracted = wait_for_job(service, service.extract(document["id"])["job_id"])
    assert extracted["status"] == "completed", extracted
    return document


def _approve(service: LabService, document_id: str) -> None:
    job = wait_for_job(service, service.approve(document_id)["job_id"])
    assert job["status"] == "completed", job


def _long_pdf_text() -> str:
    filler = "\n\n".join(f"記録段落{index}：架空の運用手順と観測の準備を整理した。" for index in range(60))
    return "架空の展示運用記録\n\n暁星帯の安全札は青色です。\n\n" + filler + "\n\n海霧帯の安全札は白色です。"


def test_distant_facts_from_one_pdf_page_both_reach_fake_answer(
    service: LabService, fake_models: FakeModels
) -> None:
    document = _upload_and_extract(service, "架空の運用記録.pdf", _pdf_bytes())
    unit = service.document(document["id"])["units"][0]
    assert unit["kind"] == "pdf_text"
    assert unit["page_number"] == 1

    reviewed_text = _long_pdf_text()
    service.edit_unit(unit["id"], reviewed_text)
    pieces = chunk_text(reviewed_text, "pdf_text")
    assert len(pieces) >= 2
    assert any("暁星帯の安全札は青色" in piece for piece in pieces)
    assert any("海霧帯の安全札は白色" in piece for piece in pieces)
    assert not any("暁星帯の安全札は青色" in piece and "海霧帯の安全札は白色" in piece for piece in pieces)
    _approve(service, document["id"])

    def answer_using_two_sources(question: str, evidence: list[dict[str, str]]) -> dict[str, Any]:
        fake_models.answer_calls.append((question, evidence))
        first = next((item["id"] for item in evidence if "暁星帯の安全札は青色" in item["text"]), None)
        second = next((item["id"] for item in evidence if "海霧帯の安全札は白色" in item["text"]), None)
        if first is None or second is None:
            return {"answer": "", "source_ids": []}
        return {"answer": "暁星帯は青色、海霧帯は白色です。", "source_ids": [first, second]}

    fake_models.answer = answer_using_two_sources  # type: ignore[method-assign]
    result = service.ask("暁星帯と海霧帯の安全札の色は？")
    assert result["status"] == "answered", result
    assert len(result["sources"]) == 2
    assert len({source["chunk_id"] for source in result["sources"]}) == 2
    assert {source["unit_id"] for source in result["sources"]} == {unit["id"]}
    assert all(source["page_number"] == 1 for source in result["sources"])
    assert all(source["original_url"].endswith("#page=1") for source in result["sources"])


def test_image_table_remains_one_chunk_with_headers_values_and_units(
    service: LabService, fake_models: FakeModels
) -> None:
    table = "GX-418 架空の比較表\n| 指標 | 測定値 | 単位 |\n|---|---:|---|\n| 水温 | 31 | ℃ |\n| 流量 | 7.2 | L/分 |"
    fake_models.image_text = table
    document = _upload_and_extract(service, "架空の表.png", png_bytes())
    assert chunk_text(table, "image_interpretation") == [table]
    _approve(service, document["id"])
    with service.db.connect() as connection:
        chunks = connection.execute(
            "SELECT search_text, citation_text FROM chunks WHERE document_id=?", (document["id"],)
        ).fetchall()
    assert len(chunks) == 1
    assert chunks[0]["search_text"] == table
    assert chunks[0]["citation_text"] == table
    answer = service.ask("GX-418 の水温は？")
    assert answer["status"] == "answered", answer
    assert table in answer["sources"][0]["snippet"]


def test_long_image_table_is_not_split_at_an_arbitrary_character_limit() -> None:
    header = "| 項目 | 値 | 単位 |\n|---|---:|---|\n"
    rows = "".join(f"| 架空の観測{index} | {index} | ℃ |\n" for index in range(180))
    table = header + rows
    assert len(table) > 3000
    assert chunk_text(table, "image_interpretation") == [table.strip()]


def test_pdf_split_retains_literal_context_overlap() -> None:
    first = "架空の前段落です。" * 80
    second = "架空の後段落です。" * 80
    pieces = chunk_text(first + "\n\n" + second, "pdf_text")
    assert len(pieces) >= 2
    assert all(len(piece) <= 1000 for piece in pieces)
    assert pieces[0][-70:] in pieces[1]


def test_long_audio_segment_keeps_its_original_coarse_time_range(
    service: LabService, fake_asr: FakeAsr, fake_models: FakeModels
) -> None:
    transcript = "\n\n".join(
        f"架空の案内段落{index}では、展示の見方を順に説明します。" for index in range(80)
    )
    fake_asr.segments = [{"start_sec": 0.25, "end_sec": 1.75, "text": transcript}]
    document = _upload_and_extract(service, "架空の音声.wav", wav_bytes(seconds=2))
    unit = service.document(document["id"])["units"][0]
    assert unit["kind"] == "audio_transcript"
    assert unit["start_sec"] == 0.25
    assert unit["end_sec"] == 1.75
    assert len(chunk_text(transcript, "audio_transcript")) >= 2
    _approve(service, document["id"])
    with service.db.connect() as connection:
        chunks = connection.execute(
            "SELECT c.id, c.unit_id, u.start_sec, u.end_sec FROM chunks c "
            "JOIN extracted_units u ON c.unit_id=u.id WHERE c.document_id=? ORDER BY c.ordinal",
            (document["id"],),
        ).fetchall()
    assert len(chunks) >= 2
    assert all(row["unit_id"] == unit["id"] for row in chunks)
    assert all(row["start_sec"] == 0.25 and row["end_sec"] == 1.75 for row in chunks)

    def cite_transcript(question: str, evidence: list[dict[str, str]]) -> dict[str, Any]:
        fake_models.answer_calls.append((question, evidence))
        if not evidence:
            return {"answer": "", "source_ids": []}
        return {"answer": "展示の見方を順に説明しています。", "source_ids": [evidence[0]["id"]]}

    fake_models.answer = cite_transcript  # type: ignore[method-assign]
    answer = service.ask("展示の見方を教えて")
    assert answer["status"] == "answered", answer
    assert "展示の見方" in fake_models.answer_calls[-1][1][0]["text"]
    assert answer["sources"][0]["start_sec"] == 0.25
    assert answer["sources"][0]["end_sec"] == 1.75
    assert answer["sources"][0]["original_url"].endswith("#t=0.25")


def test_late_reindex_embedding_failure_preserves_old_chunk_rows(
    service: LabService, fake_models: FakeModels
) -> None:
    document = _upload_and_extract(service, "架空の再索引.pdf", _pdf_bytes())
    unit = service.document(document["id"])["units"][0]
    service.edit_unit(unit["id"], _long_pdf_text())
    _approve(service, document["id"])
    with service.db.connect() as connection:
        before = [dict(row) for row in connection.execute(
            "SELECT * FROM chunks WHERE document_id=? ORDER BY ordinal", (document["id"],)
        )]
    assert len(before) >= 2

    original_embed = fake_models.embed
    calls = 0

    def fail_after_first_chunk(text: str) -> list[float]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ModelError("架空の二件目埋め込み失敗")
        return original_embed(text)

    fake_models.embed = fail_after_first_chunk  # type: ignore[method-assign]
    failed = wait_for_job(service, service.reindex(document["id"])["job_id"])
    fake_models.embed = original_embed  # type: ignore[method-assign]
    assert failed["status"] == "failed"
    assert calls == 2
    assert service.document(document["id"])["status"] == "indexed"
    with service.db.connect() as connection:
        after = [dict(row) for row in connection.execute(
            "SELECT * FROM chunks WHERE document_id=? ORDER BY ordinal", (document["id"],)
        )]
    assert after == before


def test_reindex_replaces_only_the_selected_documents_chunks(service: LabService) -> None:
    target = _upload_and_extract(service, "架空の対象.pdf", _pdf_bytes())
    other = _upload_and_extract(service, "架空の別資料.png", png_bytes())
    target_unit = service.document(target["id"])["units"][0]
    service.edit_unit(target_unit["id"], _long_pdf_text())
    _approve(service, target["id"])
    _approve(service, other["id"])
    with service.db.connect() as connection:
        before_target = [dict(row) for row in connection.execute(
            "SELECT * FROM chunks WHERE document_id=? ORDER BY ordinal", (target["id"],)
        )]
        before_other = [dict(row) for row in connection.execute(
            "SELECT * FROM chunks WHERE document_id=? ORDER BY ordinal", (other["id"],)
        )]
    assert len(before_target) >= 2

    reindexed = wait_for_job(service, service.reindex(target["id"])["job_id"])
    assert reindexed["status"] == "completed"
    with service.db.connect() as connection:
        after_target = [dict(row) for row in connection.execute(
            "SELECT * FROM chunks WHERE document_id=? ORDER BY ordinal", (target["id"],)
        )]
        after_other = [dict(row) for row in connection.execute(
            "SELECT * FROM chunks WHERE document_id=? ORDER BY ordinal", (other["id"],)
        )]
    assert [row["search_text"] for row in after_target] == [row["search_text"] for row in before_target]
    assert {row["id"] for row in after_target}.isdisjoint({row["id"] for row in before_target})
    assert {row["unit_id"] for row in after_target} == {target_unit["id"]}
    assert after_other == before_other
