# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

"""Page-level retrieval; only approved SQLite text can become answer evidence."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Protocol

from PIL import Image, ImageOps

from .advanced_retrieval import RRF_K
from .retrieval import lexical_score, normalize, terms

# Experimental floor for the pinned Japanese CLIP visual space, not a probability.
VISUAL_MIN_COSINE = 0.28
VISUAL_EXACT_ID_MIN_COSINE = 0.20
PageKey = tuple[str, int, str]


class VisualEncoder(Protocol):
    @property
    def model_id(self) -> str: ...
    @property
    def dimension(self) -> int: ...
    def encode_image(self, image: Image.Image) -> list[float]: ...
    def encode_text(self, text: str) -> list[float]: ...


def page_key(row: dict[str, Any]) -> PageKey:
    # Page zero is a standalone image. Nonvisual units get separate group keys.
    page = row["page_number"]
    return (row["document_id"], page or 0, "" if page or row["mime_type"].startswith("image/") else row["unit_id"])


def visual_pages(rows: list[dict[str, Any]]) -> dict[PageKey, list[dict[str, Any]]]:
    pages: dict[PageKey, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if (row["mime_type"] == "application/pdf" and row["page_number"]) or row["mime_type"].startswith("image/"):
            pages[page_key(row)].append(row)
    return dict(pages)


def read_visual_page(path: Path, mime_type: str, page_number: int, max_side: int = 896) -> Image.Image:
    if mime_type == "application/pdf":
        import pymupdf

        with pymupdf.open(path) as document:
            if not 1 <= page_number <= document.page_count:
                raise ValueError("invalid PDF page")
            page = document[page_number - 1]
            side = max(page.rect.width, page.rect.height)
            if not math.isfinite(side) or side <= 0:
                raise ValueError("invalid PDF dimensions")
            scale = min(2.0, max_side / side)
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csRGB, alpha=False)
            return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    with Image.open(path) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        return image


def visual_rank(
    question: str,
    pages: dict[PageKey, list[dict[str, Any]]],
    hits: list[tuple[str, float]],
) -> list[dict[str, Any]]:
    """Map ranked pages to the closest approved text within each page."""
    groups = {items[0]["id"]: items for items in pages.values()}
    query_terms = terms(question)
    identifiers = {word for word in re.findall(r"[a-z][a-z0-9_.-]*", normalize(question)) if any(ch.isdigit() for ch in word)}
    ranked = []
    for chunk_id, score in hits:
        floor = VISUAL_EXACT_ID_MIN_COSINE if identifiers else VISUAL_MIN_COSINE
        if not math.isfinite(score) or score < floor or chunk_id not in groups:
            continue
        matching = []
        for row in groups[chunk_id]:
            if identifiers and not identifiers.issubset(terms(row["search_text"])):
                continue
            overlap = lexical_score(query_terms, terms(row["search_text"]))
            matching.append({**row, "lexical_score": overlap, "visual_score": score, "retrieval_origin": "visual"})
        if matching:
            ranked.append(max(matching, key=lambda row: row["lexical_score"]))
    return ranked[:5]


def fuse_pages(
    text: list[dict[str, Any]], visual: list[dict[str, Any]], limit: int = 5,
) -> list[dict[str, Any]]:
    """Each channel contributes once per page; raw text/visual scores stay apart."""
    scores: dict[PageKey, float] = defaultdict(float)
    groups: dict[PageKey, list[dict[str, Any]]] = defaultdict(list)
    for ranking in (text, visual):
        seen: set[PageKey] = set()
        for row in ranking:
            key = page_key(row)
            if key not in seen:
                seen.add(key)
                scores[key] += 1 / (RRF_K + len(seen))
            if all(old["id"] != row["id"] for old in groups[key]):
                groups[key].append(row)
    keys = sorted(groups, key=lambda key: -scores[key])
    # One excerpt per page first, then keep room for extra text facts when possible.
    ordered = [groups[key][0] for key in keys]
    ordered.extend(groups[key][1] for key in keys if len(groups[key]) > 1)
    return ordered[:limit]
