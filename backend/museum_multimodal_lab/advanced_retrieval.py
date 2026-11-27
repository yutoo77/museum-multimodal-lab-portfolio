# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

"""Small, explainable text retrieval modes alongside the frozen legacy ranker.

SQLite supplies the current, approved corpus. Qdrant supplies only dense
candidate ordering; its payload is never treated as answer evidence.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Any, Final

from .retrieval import QUESTION_NOISE, _duplicate_key, lexical_score, normalize, terms

DEFAULT_RETRIEVAL_MODE: Final = "bm25"
RETRIEVAL_MODES = (DEFAULT_RETRIEVAL_MODE, "legacy", "dense", "hybrid")
BM25_K1 = 1.2
# These are bounded chunks, not full documents. b=.5 avoids over-penalizing
# a relevant 1000-character PDF passage against an unrelated short note.
BM25_B = 0.5
RRF_K = 60
LEXICAL_GATE = 0.08
DENSE_GATE = 0.72
DENSE_IDENTIFIER_ANCHORED_GATE = 0.45
BM25_STOPWORDS = {"の", "は", "を", "に", "で", "と", "が", "も", "か", "何", "です", "ですか", "ます", "する", "した"}


def bm25_tokens(text: str) -> list[str]:
    """Tokenize Japanese as character bigrams and ASCII identifiers as words.

    This deliberately does not claim morphological word segmentation. Numeric
    identifiers remain intact, while Japanese inflections can still overlap.
    """
    result: list[str] = []
    for piece in re.findall(r"[a-z0-9][a-z0-9_.-]*|[一-龥]+|[ぁ-ゖ]+|[ァ-ヺー]+", normalize(text)):
        if piece in QUESTION_NOISE or piece in BM25_STOPWORDS:
            continue
        if re.match(r"[a-z0-9]", piece) or len(piece) == 1:
            result.append(piece)
        else:
            result.extend(piece[index:index + 2] for index in range(len(piece) - 1))
    return result


def bm25_scores(question: str, rows: list[dict[str, Any]]) -> dict[str, float]:
    """Robertson BM25 over the *current* SQLite chunks, not a cached corpus."""
    if not rows:
        return {}
    query_terms = set(bm25_tokens(question))
    documents = [(row["id"], Counter(bm25_tokens(row["search_text"]))) for row in rows]
    lengths = [sum(counts.values()) for _, counts in documents]
    avg_length = sum(lengths) / len(lengths) if any(lengths) else 1.0
    frequencies = Counter(token for _, counts in documents for token in counts)
    size = len(documents)
    scores: dict[str, float] = {}
    for (chunk_id, counts), length in zip(documents, lengths, strict=True):
        score = 0.0
        for token in query_terms:
            tf = counts[token]
            if not tf:
                continue
            df = frequencies[token]
            idf = math.log1p((size - df + 0.5) / (df + 0.5))
            score += idf * tf * (BM25_K1 + 1) / (tf + BM25_K1 * (1 - BM25_B + BM25_B * length / avg_length))
        scores[chunk_id] = score
    return scores


def _eligible_rows(question: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    identifiers = [word for word in re.findall(r"[a-z][a-z0-9_.-]*", normalize(question)) if any(char.isdigit() for char in word)]
    if not identifiers:
        return rows
    return [row for row in rows if set(identifiers).issubset(terms(row["search_text"]))]


def _diverse_top(question: str, scored: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """At most two distinct passages per Unit; prefer new query-term coverage."""
    query_terms = terms(question)
    by_unit: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in sorted(scored, key=lambda item: (-item["rank_score"], item["id"])):
        by_unit[item["unit_id"]].append(item)
    chosen: list[dict[str, Any]] = []
    for candidates in by_unit.values():
        first = candidates[0]
        chosen.append(first)
        first_terms = terms(first["search_text"])
        distinct = [item for item in candidates[1:] if _duplicate_key(item["search_text"]) != _duplicate_key(first["search_text"])]
        if distinct:
            priorities = []
            for item in distinct:
                candidate_terms = terms(item["search_text"])
                novel = sum(weight for token, weight in query_terms.items() if candidate_terms[token] and not first_terms[token])
                priorities.append((novel, item["rank_score"], item))
            chosen.append(max(priorities, key=lambda priority: (priority[0], priority[1]))[2])
    chosen.sort(key=lambda item: (-item["rank_score"], item["id"]))
    return chosen[:max(0, limit)]


def rank_text_modes(
    question: str,
    rows: list[dict[str, Any]],
    mode: str,
    dense_hits: list[tuple[str, float]] | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Rank BM25, Qdrant cosine, or RRF of their independent rank lists.

    ``dense_hits`` are Qdrant point IDs/scores, never source text. The rows
    passed here must already be current approved SQLite rows.
    """
    if mode not in {"bm25", "dense", "hybrid"}:
        raise ValueError("unsupported retrieval mode")
    eligible_rows = _eligible_rows(question, rows)
    if not eligible_rows:
        return []
    row_by_id = {row["id"]: row for row in eligible_rows}
    lexical_query = terms(question)
    lexical_overlap = {row["id"]: lexical_score(lexical_query, terms(row["search_text"])) for row in eligible_rows}
    bm25 = bm25_scores(question, eligible_rows) if mode != "dense" else {}
    bm25_rank = [chunk_id for chunk_id in sorted(bm25, key=lambda chunk_id: (-bm25[chunk_id], chunk_id)) if bm25[chunk_id] > 0 and lexical_overlap[chunk_id] >= LEXICAL_GATE]
    identifiers = {word for word in re.findall(r"[a-z][a-z0-9_.-]*", normalize(question)) if any(char.isdigit() for char in word)}
    subject_tokens = set(bm25_tokens(question)) - identifiers
    dense: dict[str, float] = {}
    for chunk_id, score in dense_hits or []:
        if chunk_id not in row_by_id or not math.isfinite(score):
            continue
        # The lower floor is only for a passage containing the exact exhibit
        # identifier *and* a non-identifier subject term from the question.
        # This helps a long PDF's separate facts without letting the ID alone
        # license an unrelated answer. Scores are not calibrated probabilities.
        anchored = bool(identifiers and subject_tokens.intersection(bm25_tokens(row_by_id[chunk_id]["search_text"])))
        gate = DENSE_IDENTIFIER_ANCHORED_GATE if anchored else DENSE_GATE
        if score >= gate:
            dense[chunk_id] = score
    dense_rank = [chunk_id for chunk_id in sorted(dense, key=lambda chunk_id: (-dense[chunk_id], chunk_id))]
    if mode == "bm25":
        candidates = bm25_rank
        combined = {chunk_id: bm25[chunk_id] for chunk_id in candidates}
    elif mode == "dense":
        candidates = dense_rank
        combined = {chunk_id: dense[chunk_id] for chunk_id in candidates}
    else:
        combined: dict[str, float] = {}
        for ranking in (bm25_rank, dense_rank):
            for position, chunk_id in enumerate(ranking, 1):
                combined[chunk_id] = combined.get(chunk_id, 0.0) + 1.0 / (RRF_K + position)
        candidates = list(combined)
    scored = [
        {
            **row_by_id[chunk_id],
            "lexical_score": lexical_overlap[chunk_id],  # legacy overlap scale for clarification
            "bm25_score": bm25.get(chunk_id),
            "dense_score": dense.get(chunk_id),
            "rrf_score": combined[chunk_id] if mode == "hybrid" else None,
            "rank_score": combined[chunk_id],
            "retrieval_mode": mode,
        }
        for chunk_id in candidates
    ]
    return _diverse_top(question, scored, limit)
