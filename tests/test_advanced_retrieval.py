# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

"""Deterministic BM25/RRF checks; no Ollama, Qdrant, or live documents."""

from __future__ import annotations

from museum_multimodal_lab.advanced_retrieval import (
    bm25_scores,
    bm25_tokens,
    rank_text_modes,
)
from museum_multimodal_lab.retrieval import normalize


def row(chunk_id: str, unit_id: str, text: str) -> dict[str, str]:
    return {"id": chunk_id, "unit_id": unit_id, "search_text": text}


def test_bm25_keeps_identifiers_and_japanese_bigrams() -> None:
    tokens = bm25_tokens("LM-482 の水温は17℃")
    assert "lm-482" in tokens
    assert "17" in tokens
    assert "水温" in tokens


def test_pdf_figure_dash_matches_ascii_exhibit_identifier() -> None:
    assert normalize("LM‒482") == "lm-482"
    rows = [row("right", "a", "LM‒482 の水温は17℃です"), row("wrong", "b", "LM‒483 の水温は22℃です")]
    assert [item["id"] for item in rank_text_modes("LM-482 の水温は？", rows, "bm25")] == ["right"]


def test_bm25_uses_corpus_frequency_term_frequency_and_length() -> None:
    rows = [
        row("short", "a", "星雲温度 星雲温度 LM-482"),
        row("long", "b", "星雲温度 " + "背景説明 " * 40),
        row("none", "c", "別の展示資料"),
    ]
    scores = bm25_scores("星雲温度", rows)
    assert scores["short"] > scores["long"] > 0
    assert scores["none"] == 0


def test_bm25_does_not_retrieve_only_matching_question_grammar() -> None:
    rows = [row("known", "a", "架空の水槽は17℃です"), row("other", "b", "架空の光路は青です")]
    assert rank_text_modes("恐竜化石の重量は何kgですか？", rows, "bm25") == []


def test_bm25_selects_distant_passages_same_unit_and_suppresses_duplicate() -> None:
    rows = [
        row("temperature", "pdf-page", "架空展示の水温は17℃です。"),
        row("overlap", "pdf-page", "架空展示 の水温は17℃です"),
        row("flow", "pdf-page", "架空展示の流量は42Lです。"),
        row("unrelated", "other", "別館の開館時間です。"),
    ]
    selected = rank_text_modes("架空展示の水温と流量は？", rows, "bm25")
    assert "flow" in {item["id"] for item in selected}
    assert len({item["id"] for item in selected} & {"temperature", "overlap"}) == 1


def test_dense_uses_qdrant_order_but_rejects_stale_ids_and_unrelated_hits() -> None:
    rows = [row("right", "a", "LM-482の水温は17℃です"), row("wrong", "b", "LM-483の水温は22℃です")]
    selected = rank_text_modes("LM-482の水温は？", rows, "dense", [("missing-old", 0.99), ("wrong", 0.98), ("right", 0.81)])
    assert [item["id"] for item in selected] == ["right"]
    assert rank_text_modes("無関係な質問", rows, "dense", [("right", 0.3)]) == []


def test_dense_lower_floor_requires_identifier_and_subject_term() -> None:
    rows = [
        row("water", "a", "LM-482 の水温は17℃です"),
        row("unrelated", "b", "LM-482 の開館時間を確認してください"),
    ]
    hits = [("unrelated", 0.68), ("water", 0.49)]
    assert [item["id"] for item in rank_text_modes("LM-482の水温は？", rows, "dense", hits)] == ["water"]
    assert rank_text_modes("LM-482の入館料金は？", rows, "dense", hits) == []
    assert rank_text_modes("水温は？", rows, "dense", [("water", 0.49)]) == []


def test_hybrid_rrf_is_rank_based_and_keeps_score_types_separate() -> None:
    rows = [
        row("a", "a", "LM-482の水温は17℃です"),
        row("b", "b", "LM-483の水温は22℃です"),
        row("c", "c", "別の資料です"),
    ]
    selected = rank_text_modes("水温は何℃ですか？", rows, "hybrid", [("b", 0.91), ("a", 0.82), ("c", 0.2)])
    assert selected
    assert selected[0]["rrf_score"] > 0
    assert selected[0]["bm25_score"] is not None
    assert selected[0]["dense_score"] is not None
    assert selected[0]["lexical_score"] <= 1
