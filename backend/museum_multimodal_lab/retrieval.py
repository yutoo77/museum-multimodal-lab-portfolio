# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from typing import Any

QUESTION_NOISE = {"について", "教えて", "ください", "ですか", "何です", "なぜ", "どの", "とは", "方法", "資料", "展示"}
PDF_CHUNK_CHARS = 1000
AUDIO_CHUNK_CHARS = 1600
CHUNK_OVERLAP_CHARS = 120
CLARIFICATION_MIN_LEXICAL = 0.25
CLARIFICATION_RELATIVE_LEXICAL = 0.8
COMPARISON_WORDS = ("比較", "違い", "それぞれ", "各資料", "両方", "双方", "共通点", "合計")
HYPHEN_VARIANTS = str.maketrans({character: "-" for character in "‐‑‒–—−﹣"})


def normalize(text: str) -> str:
    # PDF fonts can extract a visually ordinary identifier hyphen as U+2012
    # (figure dash). Treat typographic dashes as the same identifier separator.
    return unicodedata.normalize("NFKC", text).translate(HYPHEN_VARIANTS).lower()


def terms(text: str) -> Counter[str]:
    clean = normalize(text)
    result: Counter[str] = Counter()
    for piece in re.findall(r"[a-z0-9][a-z0-9_.-]*|[一-龥ぁ-ゖァ-ヺー]{2,}", clean):
        if piece in QUESTION_NOISE:
            continue
        if re.match(r"[a-z0-9]", piece):
            result[piece] += 2
        else:
            for index in range(len(piece) - 1):
                gram = piece[index : index + 2]
                if gram not in QUESTION_NOISE:
                    result[gram] += 1
    return result


def lexical_score(query: Counter[str], document: Counter[str]) -> float:
    if not query or not document:
        return 0.0
    overlap = sum(min(count, document[term]) for term, count in query.items())
    return overlap / max(1.0, sum(query.values()))


def cosine(a: list[float], b: list[float]) -> float:
    if not a or len(a) != len(b):
        return -1.0
    if any(not math.isfinite(value) for value in a + b):
        return -1.0
    denominator = math.sqrt(sum(value * value for value in a) * sum(value * value for value in b))
    return sum(x * y for x, y in zip(a, b)) / denominator if denominator else -1.0


def _duplicate_key(text: str) -> tuple[str, tuple[str, ...]]:
    """Ignore spacing/punctuation duplicates, but retain numeric/ASCII distinctions.

    The second part prevents e.g. 17.5 and 175 from collapsing when punctuation
    is removed. Distinct source facts should never be suppressed by fuzzy matching.
    """
    clean = normalize(text)
    words_and_numbers = tuple(re.findall(r"[a-z][a-z0-9_.-]*|\d+(?:[.,]\d+)?", clean))
    compact = "".join(char for char in clean if not char.isspace() and not unicodedata.category(char).startswith("P"))
    return compact, words_and_numbers


def rank_chunks(question: str, query_vector: list[float], rows: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    """RRF of local dense and lexical ranks, with an abstention gate.

    Exact identifiers in a question must occur in the retrieved text. This
    avoids using a semantically similar but different exhibit's numeric value.
    """
    query_terms = terms(question)
    identifiers = [word for word in re.findall(r"[a-z][a-z0-9_.-]*", normalize(question)) if any(ch.isdigit() for ch in word)]
    scored = []
    for row in rows:
        text = normalize(row["search_text"])
        if identifiers and not all(identifier in text for identifier in identifiers):
            continue
        lexical = lexical_score(query_terms, terms(text))
        try:
            vector = json.loads(row["embedding_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        dense = cosine(query_vector, vector)
        scored.append((row, lexical, dense))
    if not scored:
        return []
    # These are relevance gates, not evidence of semantic correctness.
    eligible = [item for item in scored if item[1] >= 0.08 or item[2] >= 0.72]
    if not eligible:
        return []
    lexical_rank = sorted(eligible, key=lambda item: item[1], reverse=True)
    dense_rank = sorted(eligible, key=lambda item: item[2], reverse=True)
    combined: dict[str, float] = {}
    for ranking in (lexical_rank, dense_rank):
        for position, (row, _, _) in enumerate(ranking, 1):
            combined[row["id"]] = combined.get(row["id"], 0) + 1 / (60 + position)
    ordered = sorted(eligible, key=lambda item: combined[item[0]["id"]], reverse=True)
    by_unit: dict[str, list[tuple[dict[str, Any], float, float]]] = defaultdict(list)
    for item in ordered:
        by_unit[item[0]["unit_id"]].append(item)

    # Keep the best RRF hit for each Unit. For its second hit, prefer new query
    # terms rather than an overlapping passage with a slightly higher RRF.
    # This is a small, explainable diversity rule; it is not vector MMR.
    def second_priority(item: tuple[dict[str, Any], float, float], first_terms: Counter[str]) -> tuple[int, float]:
        candidate_terms = terms(item[0]["search_text"])
        novel = sum(weight for term, weight in query_terms.items() if candidate_terms[term] and not first_terms[term])
        return novel, combined[item[0]["id"]]

    chosen = []
    for candidates in by_unit.values():
        first = candidates[0]
        chosen.append(first)
        if len(candidates) < 2:
            continue
        first_key = _duplicate_key(first[0]["search_text"])
        first_terms = terms(first[0]["search_text"])

        distinct = [item for item in candidates[1:] if _duplicate_key(item[0]["search_text"]) != first_key]
        if distinct:
            chosen.append(max(distinct, key=lambda item: second_priority(item, first_terms)))

    chosen.sort(key=lambda item: combined[item[0]["id"]], reverse=True)
    return [
        {**row, "lexical_score": lexical, "dense_score": dense, "rrf_score": combined[row["id"]]}
        for row, lexical, dense in chosen[:max(0, limit)]
    ]


def clarification_candidates(question: str, ranked: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Find close, *distinct current-document* hits that need exhibit selection.

    This intentionally does not guess which conflicting exhibit identifier a
    vague question meant. It is a conservative guard for strong lexical ties,
    not a general entity resolver or a replacement for the existing ranking.
    """
    clean_question = normalize(question)
    if any(word in clean_question for word in COMPARISON_WORDS):
        return []
    if any(any(char.isdigit() for char in word) for word in re.findall(r"[a-z][a-z0-9_.-]*", clean_question)):
        return []

    # Two chunks or two versions of the same document must not create a false
    # choice. The service only supplies current indexed documents to rank_chunks.
    best_by_document: dict[str, dict[str, Any]] = {}
    for row in ranked:
        best_by_document.setdefault(row["document_id"], row)
    candidates = [row for row in best_by_document.values() if row["lexical_score"] >= CLARIFICATION_MIN_LEXICAL]
    if len(candidates) < 2:
        return []

    strongest_lexical = max(row["lexical_score"] for row in candidates)
    close = [row for row in candidates if row["lexical_score"] >= CLARIFICATION_RELATIVE_LEXICAL * strongest_lexical]
    if len(close) < 2:
        return []

    # Use only explicit exhibit-like identifiers in the indexed *content*, not
    # filenames or generated model text. Hyphen/underscore and a two-digit
    # suffix avoid treating ordinary version labels or quantities as exhibits.
    def identifiers(row: dict[str, Any]) -> set[str]:
        return set(re.findall(r"(?<![a-z0-9])[a-z]{2,}[-_]\d{2,}(?![a-z0-9])", normalize(row["citation_text"])))

    distinct: list[dict[str, Any]] = []
    seen_identifiers: set[str] = set()
    for row in close:
        row_identifiers = identifiers(row)
        if row_identifiers and seen_identifiers.isdisjoint(row_identifiers):
            distinct.append(row)
            seen_identifiers.update(row_identifiers)
        if len(distinct) == 3:
            break
    return distinct if len(distinct) >= 2 else []


def chunk_text(text: str, kind: str, max_chars: int | None = None) -> list[str]:
    """Split long PDF/ASR units without inventing source locations or table structure.

    Each returned piece still belongs to the original extracted Unit. In
    particular, split ASR text retains the Unit's *coarse* time range; this
    function cannot infer a timestamp for an individual sentence.
    """
    content = text.strip()
    if not content:
        return []
    if kind not in {"pdf_text", "audio_transcript"}:
        return [content]
    target = max_chars if max_chars is not None else (PDF_CHUNK_CHARS if kind == "pdf_text" else AUDIO_CHUNK_CHARS)
    if target <= 0:
        raise ValueError("max_chars must be positive")
    if len(content) <= target:
        return [content]

    # Prefer a paragraph, then sentence, then line boundary near the end of a
    # window. If none exists, cut at the hard limit. The overlap is literal
    # source text, not a generated summary or guessed heading.
    overlap = min(CHUNK_OVERLAP_CHARS, max(0, target // 6))
    chunks: list[str] = []
    start = 0
    while start < len(content):
        end = min(start + target, len(content))
        if end < len(content):
            boundary_start = start + max(1, int(target * 0.7))
            window = content[boundary_start:end]
            for pattern in (r"\n\s*\n", r"[。！？!?]", r"\n"):
                boundaries = list(re.finditer(pattern, window))
                if boundaries:
                    end = boundary_start + boundaries[-1].end()
                    break
        piece = content[start:end].strip()
        if piece:
            chunks.append(piece)
        if end == len(content):
            break
        start = max(start + 1, end - overlap)
    return chunks
