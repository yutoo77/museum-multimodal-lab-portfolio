# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import io

from conftest import FakeModels, png_bytes, wait_for_job
from museum_multimodal_lab.service import LabService
from PIL import Image


def register_image(service: LabService, name: str, text: str, data: bytes | None = None) -> dict:
    document = service.upload(name, data if data is not None else png_bytes())
    assert wait_for_job(service, service.extract(document["id"])["job_id"])["status"] == "completed"
    unit = service.document(document["id"])["units"][0]
    service.edit_unit(unit["id"], text)
    assert wait_for_job(service, service.approve(document["id"])["job_id"])["status"] == "completed"
    return service.document(document["id"])


def test_ambiguous_exhibit_question_requests_clarification_from_current_sources(
    service: LabService, fake_models: FakeModels
) -> None:
    first = register_image(service, "架空ルミナ表.png", "架空展示ルミナ水槽。識別番号 LM-482。水温は17℃です。")
    second = register_image(service, "架空ルミナ記録.png", "架空展示ルミナ水槽。識別番号 LM-484。水温は22℃です。")

    result = service.ask("ルミナ水槽の水温は何℃ですか？")

    assert result["status"] == "needs_clarification"
    assert "識別番号" in result["answer"]
    assert {item["document_id"] for item in result["sources"]} == {first["id"], second["id"]}
    assert {item["snippet"] for item in result["sources"]} == {
        "架空展示ルミナ水槽。識別番号 LM-482。水温は17℃です。",
        "架空展示ルミナ水槽。識別番号 LM-484。水温は22℃です。",
    }
    assert all(item["original_url"] == f"/api/documents/{item['document_id']}/original" for item in result["sources"])
    assert fake_models.answer_calls == []  # No arbitrary model choice on an unresolved target.

    specific = service.ask("ルミナ水槽 LM-482 の水温は何℃ですか？")
    assert specific["status"] == "answered"
    assert {item["document_id"] for item in specific["sources"]} == {first["id"]}
    assert "17" in specific["answer"]


def test_comparison_question_is_not_clarified(service: LabService, fake_models: FakeModels) -> None:
    register_image(service, "比較A.png", "架空展示ルミナ水槽。識別番号 LM-482。水温は17℃です。")
    register_image(service, "比較B.png", "架空展示ルミナ水槽。識別番号 LM-484。水温は22℃です。")

    result = service.ask("ルミナ水槽の水温をそれぞれ比較してください。")

    assert result["status"] == "answered"
    assert len(fake_models.answer_calls) == 1
    assert {item["name"] for item in fake_models.answer_calls[0][1]} == {"比較A.png", "比較B.png"}


def test_short_shared_topic_with_distinct_identifiers_asks_which_document(
    service: LabService, fake_models: FakeModels
) -> None:
    register_image(service, "水槽A.png", "架空展示ルミナ水槽。識別番号 LM-482。水温は17℃です。")
    register_image(service, "水槽B.png", "架空展示ニナミ水槽。識別番号 LM-483。水温は22℃です。")

    result = service.ask("水槽の水温を教えて", "bm25")

    assert result["status"] == "needs_clarification"
    assert len(result["sources"]) == 2
    assert fake_models.answer_calls == []


def test_same_exhibit_identifier_in_two_documents_does_not_overclarify(
    service: LabService, fake_models: FakeModels
) -> None:
    register_image(service, "同じ展示A.png", "架空展示ルミナ水槽。識別番号 LM-482。水温は17℃です。")
    register_image(service, "同じ展示B.png", "架空展示ルミナ水槽。識別番号 LM-482。水温は17℃です。")

    result = service.ask("ルミナ水槽の水温は何℃ですか？")

    assert result["status"] == "answered"
    assert len(fake_models.answer_calls) == 1


def test_weakly_related_document_does_not_force_clarification(service: LabService) -> None:
    target = register_image(service, "水槽.png", "架空展示ルミナ水槽。識別番号 LM-482。水温は17℃です。")
    register_image(service, "星座.png", "架空展示ソラノ星座。識別番号 ST-713。星の数は22個です。")

    result = service.ask("ルミナ水槽の水温は何℃ですか？")

    assert result["status"] == "answered"
    assert {item["document_id"] for item in result["sources"]} == {target["id"]}


def test_clarification_never_shows_missing_original(service: LabService, fake_models: FakeModels) -> None:
    register_image(service, "現存.png", "架空展示ルミナ水槽。識別番号 LM-482。水温は17℃です。")
    missing = register_image(service, "消失.png", "架空展示ルミナ水槽。識別番号 LM-484。水温は22℃です。")
    original, _, _ = service.original_path(missing["id"])
    original.unlink()

    result = service.ask("ルミナ水槽の水温は何℃ですか？")

    assert result == {"status": "held", "answer": "登録資料からは確認できません。", "sources": []}
    assert fake_models.answer_calls == []


def test_superseded_version_does_not_appear_as_another_candidate(service: LabService) -> None:
    old = register_image(service, "同名.png", "架空展示ルミナ水槽。識別番号 LM-482。水温は17℃です。")
    replacement = io.BytesIO()
    Image.new("RGB", (180, 90), "lightblue").save(replacement, format="PNG")
    current = register_image(service, "同名.png", "架空展示ルミナ水槽。識別番号 LM-484。水温は22℃です。", replacement.getvalue())

    assert service.document(old["id"])["status"] == "superseded"
    result = service.ask("ルミナ水槽の水温は何℃ですか？")
    assert result["status"] == "answered"
    assert {source["document_id"] for source in result["sources"]} == {current["id"]}
