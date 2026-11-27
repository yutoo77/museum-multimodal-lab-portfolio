# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

import json

import pytest
from museum_multimodal_lab.agentic import run_agentic
from museum_multimodal_lab.providers import ModelError


def _row(identifier, text, unit=None):
    return {"id": identifier, "unit_id": unit or identifier, "citation_text": text, "search_text": text, "kind": "pdf_text"}


def _script(*actions):
    replies = iter(actions)
    return lambda _prompt, _timeout: json.dumps(next(replies), ensure_ascii=False)


def test_agentic_switches_once_and_fuses_without_leaking_content():
    question = "LM-482 水温と流量は？"
    water = _row("water", "LM-482 水温17度", "unit")
    flow = _row("flow", "LM-482 流量42 L/分", "unit")
    first, retry = [], []

    def search(query, mode, timeout):
        assert timeout > 0
        (first if mode == "bm25" else retry).append(query)
        return [water] if mode == "bm25" else [water, flow]

    result = run_agentic(question, search, _script(
        {"action": "search", "mode": "bm25"},
        {"action": "search", "mode": "hybrid", "query": "LM‒482 水温 流量"},
    ))
    assert first == [question]
    assert retry == ["LM‒482 水温 流量"]
    assert [item["id"] for item in result.ranked] == ["water", "flow"]
    assert result.stop_reason == "evidence_selected_after_retry"
    assert [event["stage"] for event in result.trace] == ["plan", "search", "plan", "search"]
    assert "LM-482" not in str(result.trace) and "水温" not in str(result.trace)


@pytest.mark.parametrize("invalid", [
    'not-json',
    '{"action":"search","mode":"external"}',
    '{"action":"search","mode":"bm25","shell":"dir"}',
    '{"action":"search","mode":"bm25","mode":"dense"}',
    '{"action":"search","mode":"bm25","query":"different"}',
    '{"action":"search","mode":["bm25"]}',
    '{"action":"search","mode":"bm25"}' + " " * 2048,
])
def test_agentic_invalid_planner_output_falls_back_to_bm25(invalid):
    question = "ルミナ 水温は？"
    calls = []

    def search(query, mode, _timeout):
        calls.append((mode, query))
        return [_row("water", "ルミナ 水温17度")]

    replies = iter((invalid, '{"action":"accept"}'))
    result = run_agentic(question, search, lambda _prompt, _timeout: next(replies))
    assert calls == [("bm25", question)]
    assert result.ranked[0]["id"] == "water"
    assert [event["status"] for event in result.trace] == ["invalid_plan", "ok", "accept"]
    assert result.trace[1]["fallback"]


@pytest.mark.parametrize("rewrite", ["LM-4820 水温 17度", "水温 17度", "LM-482 水温 18度", "LM-482 水温 17度\n\t流量"])
def test_agentic_rejects_rewrite_with_abandoned_identifiers_or_numbers(rewrite):
    question = "LM-482 水温 17度？"
    calls = []

    def search(query, mode, _timeout):
        calls.append((mode, query))
        return []

    result = run_agentic(question, search, _script(
        {"action": "search", "mode": "bm25"},
        {"action": "search", "mode": "hybrid", "query": rewrite},
    ))
    assert calls == [("bm25", question), ("hybrid", question)]
    assert result.trace[2]["status"] == "invalid_plan"
    assert result.trace[3]["fallback"]
    assert result.stop_reason == "no_evidence"


def test_agentic_rejects_substring_identifier_matches_or_empty_accept():
    result = run_agentic("LM-482 水温？", lambda *_: [_row("wrong", "LM-4820 水温20度")], _script(
        {"action": "search", "mode": "hybrid"},
        {"action": "accept"},
    ))
    assert not result.ranked
    assert result.trace[1]["hit_count"] == 0
    assert result.trace[2]["status"] == "invalid_plan"
    assert result.trace[3]["mode"] == "bm25"


def test_agentic_keeps_number_sign_and_ignores_trailing_identifier_punctuation():
    question = "水温 -17度, exhibit LM-482."
    searches = []

    def search(query, mode, _timeout):
        searches.append((query, mode))
        return [_row("a", "LM-482 水温は -17度です")]

    result = run_agentic(question, search, _script(
        {"action": "search", "mode": "bm25"},
        {"action": "search", "mode": "hybrid", "query": "LM-482 水温 17度？"},
    ))
    assert [item["id"] for item in result.ranked] == ["a"]
    assert result.trace[-1]["status"] == "invalid_plan"
    assert searches == [(question, "bm25")]


def test_agentic_respects_existing_clarification_before_rewrite():
    hits = [_row("a", "LM-482 水温17度"), _row("b", "LM-484 水温19度")]
    # A second planner call would exhaust this script and fail the test.
    result = run_agentic(
        "水温は？", lambda *_: hits, _script({"action": "search", "mode": "hybrid"}),
        stop_early=lambda ranked: len(ranked) == 2,
    )
    assert len(result.trace) == 2
    assert len(result.ranked) == 2
    assert result.stop_reason == "clarification_required"


def test_agentic_can_hold_even_when_candidates_exist():
    result = run_agentic("水温は？", lambda *_: [_row("a", "関連しない資料")], _script(
        {"action": "search", "mode": "bm25"},
        {"action": "hold"},
    ))
    assert not result.ranked
    assert result.stop_reason == "planner_held"


def test_agentic_does_not_loop_or_claim_same_evidence_is_new():
    question = "水温は？"
    hits = [_row("a", "水温17度")]
    tried_switch = run_agentic(question, lambda *_: hits, _script(
        {"action": "search", "mode": "bm25"},
        {"action": "search", "mode": "hybrid"},
    ))
    assert not tried_switch.ranked and tried_switch.stop_reason == "insufficient_after_retry"
    repeated = run_agentic(question, lambda *_: hits, _script(
        {"action": "search", "mode": "bm25"},
        {"action": "search", "mode": "bm25"},
    ))
    assert repeated.trace[-1]["status"] == "invalid_plan"
    assert len(repeated.trace) == 3 and repeated.stop_reason == "fallback_evidence"


def test_agentic_handles_model_service_failures_with_redacted_trace():
    def planner(*_):
        raise TimeoutError("private question content")

    def search(*_):
        raise ModelError("private corpus location")

    result = run_agentic("水温は？", search, planner)
    assert not result.ranked
    assert [event["status"] for event in result.trace] == [
        "planner_unavailable", "search_unavailable", "search_unavailable",
    ]
    assert result.stop_reason == "search_unavailable"
    assert "private" not in str(result.trace)


def test_agentic_explicitly_falls_back_to_bm25_if_hybrid_is_unavailable():
    def search(_query, mode, _timeout):
        if mode == "hybrid":
            raise ModelError("local Qdrant unavailable")
        return [_row("a", "水温は17度です")]

    result = run_agentic("水温は？", search, _script({"action": "search", "mode": "hybrid"}))
    assert [item["id"] for item in result.ranked] == ["a"]
    assert result.trace[1]["status"] == "search_unavailable"
    assert result.trace[2]["mode"] == "bm25" and result.trace[2]["fallback"]


def test_agentic_timeout_is_bounded_and_discards_late_results():
    now = [0.0]

    def planner(_prompt, timeout):
        assert timeout == 12.0
        now[0] += 11.0
        return '{"action":"search","mode":"hybrid"}'

    def search(_query, _mode, timeout):
        assert timeout == 4.0
        now[0] += 5.0
        return [_row("a", "水温17度")]

    result = run_agentic("水温は？", search, planner, budget_seconds=15, clock=lambda: now[0])
    assert not result.ranked
    assert result.deadline == 15.0
    assert len(result.trace) == 2
    assert result.stop_reason == "budget_exhausted"
