# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

"""Even an uncited excerpt sent to a model can influence its answer."""

from __future__ import annotations

import pytest
from conftest import png_bytes, wait_for_job

QUESTION = "架空の潮流実験 LM-482 の水温確認の手順は？"


def _approve_png(service, name, text):
    document = service.upload(name, png_bytes())
    assert wait_for_job(service, service.extract(document["id"])["job_id"])["status"] == "completed"
    unit_id = service.document(document["id"])["units"][0]["id"]
    service.edit_unit(unit_id, text)
    assert wait_for_job(service, service.approve(document["id"])["job_id"])["status"] == "completed"
    return document["id"], unit_id


@pytest.mark.parametrize("mode", ["bm25", "agentic"])
def test_ask_holds_when_an_uncited_excerpt_loses_approval_during_generation(service, fake_models, monkeypatch, mode):
    edited_name = "潮流実験の旧手順.png"
    surviving_name = "潮流実験の記録表.png"
    stale_id, stale_unit = _approve_png(
        service, edited_name, "架空の潮流実験 LM-482 の水温確認の手順です。細い管を使います。",
    )
    current_id, _ = _approve_png(
        service, surviving_name, "架空の潮流実験 LM-482 の水温確認の手順です。記録表に転記します。",
    )
    planner_responses = iter(('{"action":"search","mode":"bm25"}', '{"action":"accept"}'))
    monkeypatch.setattr(fake_models, "plan_retrieval", lambda *_: next(planner_responses), raising=False)

    def answer_with_wrong_provenance(question, excerpts):
        assert question == QUESTION
        sent = {excerpt["name"]: excerpt for excerpt in excerpts}
        assert set(sent) == {edited_name, surviving_name}
        assert "細い管" in sent[edited_name]["text"] and "細い管" not in sent[surviving_name]["text"]
        # While the model was producing an answer, the source of this fact lost
        # approval. The model nevertheless points to a different, still valid ID.
        service.edit_unit(stale_unit, "架空の潮流実験 LM-482 の水温確認の手順です。太い管を使います。")
        return {"answer": "細い管を使います。", "source_ids": [sent[surviving_name]["id"]]}

    monkeypatch.setattr(fake_models, "answer", answer_with_wrong_provenance)
    result = service.ask(QUESTION, mode)
    assert service.document(stale_id)["status"] == "draft"
    assert service.document(current_id)["status"] == "indexed"
    assert result["status"] == "held"
    assert not result["sources"]


@pytest.mark.parametrize(("approved_text", "model_answer", "status"), [
    ("予備ランプの色は白色です。", "白いです。", "answered"),
    ("予備ランプの色は白色です。", "紫色です。", "held"),
    ("予備ランプは停止時に点灯します。", "白色です。", "held"),
    ("予備ランプの色は青緑色です。", "青色です。", "held"),
])
def test_explicit_color_question_requires_literal_color_in_cited_approved_text(
    service, fake_models, monkeypatch, approved_text, model_answer, status,
):
    _approve_png(service, "架空の操作案内.png", approved_text)

    def answer(question, excerpts):
        assert "何色" in question
        assert len(excerpts) == 1
        return {"answer": model_answer, "source_ids": [excerpts[0]["id"]]}

    monkeypatch.setattr(fake_models, "answer", answer)
    result = service.ask("予備ランプは何色ですか？", "bm25")
    assert result["status"] == status
    assert bool(result["sources"]) is (status == "answered")


def test_color_as_a_search_clue_does_not_require_color_in_the_answer(service, fake_models, monkeypatch):
    _approve_png(service, "架空の操作案内.png", "白い予備ランプの識別番号は CM-209 です。")
    monkeypatch.setattr(fake_models, "answer", lambda _, excerpts: {"answer": "CM-209", "source_ids": [excerpts[0]["id"]]})
    assert service.ask("白い予備ランプの識別番号は？", "bm25")["status"] == "answered"
