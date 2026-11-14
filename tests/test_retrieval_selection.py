# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

"""Small, deterministic checks for selecting multiple pieces of one source unit.

These fixtures use fake vectors and no model, database, or saved document.
"""

from __future__ import annotations

import json
from typing import Any

from museum_multimodal_lab.retrieval import chunk_text, rank_chunks


def _row(chunk_id: str, unit_id: str, text: str, vector: list[float] | None = None) -> dict[str, Any]:
    return {
        "id": chunk_id,
        "unit_id": unit_id,
        "search_text": text,
        "embedding_json": json.dumps(vector if vector is not None else [1.0, 0.0]),
    }


def test_two_distant_facts_in_one_pdf_unit_can_both_be_selected() -> None:
    rows = [
        _row("water", "pdf-page-1", "架空展示の水温は17℃です。"),
        _row("flow", "pdf-page-1", "架空展示の流量は42L/分です。"),
        _row("other", "other-unit", "別施設の開館案内です。", [0.75, 0.66]),
    ]

    selected = rank_chunks("架空展示の水温と流量はそれぞれ何ですか？", [1.0, 0.0], rows, limit=2)

    assert {row["id"] for row in selected} == {"water", "flow"}
    assert all("rrf_score" in row for row in selected)


def test_long_pdf_does_not_choose_filler_before_a_distant_second_fact() -> None:
    filler = "\n\n".join(f"記録段落{index}：架空の運用手順と観測の準備を整理した。" for index in range(120))
    page = "架空の展示運用記録\n\n暁星帯の安全札は青色です。\n\n" + filler + "\n\n海霧帯の安全札は白色です。"
    pieces = chunk_text(page, "pdf_text")
    assert len(pieces) >= 4
    rows = [_row(str(index), "same-pdf-page", piece) for index, piece in enumerate(pieces)]

    selected = rank_chunks("暁星帯と海霧帯の安全札の色は？", [1.0, 0.0], rows, limit=5)

    assert len(selected) == 2
    assert any("暁星帯の安全札は青色" in row["search_text"] for row in selected)
    assert any("海霧帯の安全札は白色" in row["search_text"] for row in selected)


def test_same_unit_duplicate_text_does_not_displace_distinct_fact() -> None:
    rows = [
        _row("water", "pdf-page-1", "架空展示の水温は17℃。観測条件は晴天。"),
        _row("water-overlap", "pdf-page-1", "架空展示 の水温は17℃、観測条件は晴天"),
        _row("flow", "pdf-page-1", "架空展示の流量は42L/分です。"),
        _row("other", "other-unit", "別施設の開館案内です。", [0.75, 0.66]),
    ]

    selected = rank_chunks("架空展示の水温と流量は何ですか？", [1.0, 0.0], rows, limit=2)

    assert {row["id"] for row in selected} == {"water", "flow"}


def test_one_unit_cannot_fill_result_limit_with_overlapping_candidates() -> None:
    rows = [
        _row("water", "long-page", "架空展示の水温は17℃です。"),
        _row("flow", "long-page", "架空展示の流量は42L/分です。"),
        _row("duration", "long-page", "架空展示の運転時間は23分です。"),
        _row("other-a", "other-a", "別の架空展示の水温は21℃です。"),
        _row("other-b", "other-b", "もう一つの架空展示の流量は8L/分です。"),
    ]

    selected = rank_chunks("架空展示の水温と流量と運転時間を教えて", [1.0, 0.0], rows, limit=5)

    assert len(selected) <= 5
    assert sum(row["unit_id"] == "long-page" for row in selected) <= 2
    assert {"other-a", "other-b"} <= {row["id"] for row in selected}


def test_one_unit_is_limited_to_two_distinct_chunks() -> None:
    rows = [
        _row("temperature", "single-unit", "架空展示の温度は17℃です。"),
        _row("flow", "single-unit", "架空展示の流量は42L/分です。"),
        _row("duration", "single-unit", "架空展示の運転時間は23分です。"),
    ]

    selected = rank_chunks("架空展示の温度と流量と運転時間は？", [1.0, 0.0], rows, limit=5)

    assert len(selected) == 2
    assert {row["unit_id"] for row in selected} == {"single-unit"}


def test_decimal_and_integer_values_are_not_treated_as_duplicate_text() -> None:
    rows = [
        _row("decimal", "same-unit", "架空展示の試験値は17.5℃です。"),
        _row("integer", "same-unit", "架空展示の試験値は175℃です。"),
    ]

    selected = rank_chunks("架空展示の試験値はそれぞれ何℃ですか？", [1.0, 0.0], rows, limit=2)

    assert {row["id"] for row in selected} == {"decimal", "integer"}


def test_identifier_and_relevance_gates_still_hold() -> None:
    identifier_rows = [
        _row("right", "unit-1", "LM-482 の水温は17℃です。"),
        _row("wrong", "unit-2", "LM-483 の水温は19℃です。"),
    ]
    selected = rank_chunks("LM-482 の水温は？", [1.0, 0.0], identifier_rows)
    assert [row["id"] for row in selected] == ["right"]

    unrelated = [_row("unrelated", "unit-3", "別施設の開館案内です。", [0.0, 1.0])]
    assert rank_chunks("希少な鉱物の重さは？", [1.0, 0.0], unrelated) == []
