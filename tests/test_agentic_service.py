# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

"""Agentic retrieval integration with isolated SQLite and fake LLM responses."""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import FakeModels, png_bytes, wait_for_job
from museum_multimodal_lab.service import LabService

QUESTION = "架空展示 GX-418 の観測値は何ですか？"


def _register(service: LabService, *, name: str = "架空展示.png", text: str | None = None) -> tuple[str, str]:
    document = service.upload(name, png_bytes())
    document_id = document["id"]
    assert wait_for_job(service, service.extract(document_id)["job_id"])["status"] == "completed"
    unit_id = service.document(document_id)["units"][0]["id"]
    if text is not None:
        service.edit_unit(unit_id, text)
    assert wait_for_job(service, service.approve(document_id)["job_id"])["status"] == "completed"
    return document_id, unit_id


def _plan(monkeypatch: pytest.MonkeyPatch, models: FakeModels, *replies: dict | str) -> list[float]:
    remaining_replies = iter(replies)
    timeouts: list[float] = []

    def plan(_prompt: str, timeout: float) -> str:
        timeouts.append(timeout)
        reply = next(remaining_replies)
        return reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)

    monkeypatch.setattr(models, "plan_retrieval", plan, raising=False)
    return timeouts


def test_agentic_service_malformed_plan_fallback_and_source(service, fake_models, monkeypatch):
    document_id, unit_id = _register(service)
    calls = _plan(monkeypatch, fake_models, "{not JSON", {"action": "accept"})
    result = service.ask(QUESTION, "agentic")

    assert len(calls) == 2 and all(0 < timeout <= 12.0 for timeout in calls)
    assert result["status"] == "answered"
    assert "13" in result["answer"]
    assert result["sources"][0]["document_id"] == document_id
    assert result["sources"][0]["unit_id"] == unit_id
    assert len(fake_models.answer_calls) == 1 and fake_models.answer_calls[0][0] == QUESTION
    trace = result["retrieval_trace"]
    assert trace[0]["reason"] == "invalid_plan"
    assert trace[1]["mode"] == "bm25" and trace[1]["fallback"]
    assert "GX-418" not in str(trace) and "観測値" not in str(trace)


def test_agentic_service_no_rows_or_new_candidates_holds_without_generation(service, fake_models, monkeypatch):
    calls = _plan(monkeypatch, fake_models, {"action": "search", "mode": "bm25"}, {"action": "search", "mode": "hybrid"})
    pending = service.upload("未承認.png", png_bytes())
    assert wait_for_job(service, service.extract(pending["id"])["job_id"])["status"] == "completed"
    assert service.ask(QUESTION, "agentic")["status"] == "held"
    assert not calls and not fake_models.answer_calls

    _register(service)
    result = service.ask("未知の展示 ZZ-999 の観測値は何ですか？", "agentic")
    assert len(calls) == 2
    searches = [event for event in result["retrieval_trace"] if event["action"] == "search"]
    assert [(event["mode"], event["candidate_count"]) for event in searches] == [("bm25", 0), ("hybrid", 0)]
    assert result["status"] == "held" and result["sources"] == []
    assert not fake_models.answer_calls


@pytest.mark.parametrize("flag", ["forged_citation", "unverified_number"])
def test_agentic_service_rejects_unverified_answer_sources_or_numbers(service, fake_models, monkeypatch, flag):
    _register(service)
    _plan(monkeypatch, fake_models, {"action": "search", "mode": "bm25"}, {"action": "accept"})
    setattr(fake_models, flag, True)
    result = service.ask(QUESTION, "agentic")
    assert len(fake_models.answer_calls) == 1
    assert result["status"] == "held" and result["sources"] == []


def test_agentic_service_does_not_cite_approval_revoked_during_answer(service, fake_models, monkeypatch):
    document_id, unit_id = _register(service)
    _plan(monkeypatch, fake_models, {"action": "search", "mode": "bm25"}, {"action": "accept"})
    answer = fake_models.answer

    def change_during_generation(question: str, rows: list[dict[str, str]], timeout: float) -> dict[str, Any]:
        assert 0 < timeout <= 80.0
        old_answer = answer(question, rows)
        assert "13" in old_answer["answer"]
        service.edit_unit(unit_id, "架空展示 GX-418 の観測値は 31 単位です。")
        return old_answer

    monkeypatch.setattr(fake_models, "answer_bounded", change_during_generation, raising=False)
    result = service.ask(QUESTION, "agentic")
    assert len(fake_models.answer_calls) == 1
    assert service.document(document_id)["status"] == "draft"
    assert result["status"] == "held" and result["sources"] == []


def test_agentic_service_retains_existing_ambiguity_guard(service, fake_models, monkeypatch):
    first, _ = _register(service, name="水槽A.png", text="架空展示ルミナ水槽。識別番号 LM-482。水温は17℃です。")
    second, _ = _register(service, name="水槽B.png", text="架空展示ルミナ水槽。識別番号 LM-483。水温は22℃です。")
    calls = _plan(monkeypatch, fake_models, {"action": "search", "mode": "bm25"})
    result = service.ask("ルミナ水槽の水温は何℃ですか？", "agentic")
    assert len(calls) == 1  # Ambiguity must be surfaced before any planner can rewrite the subject.
    assert result["status"] == "needs_clarification"
    assert {row["document_id"] for row in result["sources"]} == {first, second}
    assert not fake_models.answer_calls


def test_agentic_service_qdrant_failure_falls_back_to_bm25(service, fake_models, monkeypatch):
    document_id, _ = _register(service)
    calls = _plan(monkeypatch, fake_models, {"action": "search", "mode": "hybrid"})

    class BrokenIndex:
        def query(self, *args, **kwargs):
            raise OSError("test-only index failure")

        def close(self):
            pass

    monkeypatch.setattr(service, "_qdrant", BrokenIndex())
    result = service.ask(QUESTION, "agentic")
    assert len(calls) == 1
    assert result["status"] == "answered"
    assert result["sources"][0]["document_id"] == document_id
    searches = [event for event in result["retrieval_trace"] if event["action"] == "search"]
    assert [(event["mode"], event["reason"], event["fallback"]) for event in searches] == [
        ("hybrid", "search_unavailable", False), ("bm25", "ok", True),
    ]
