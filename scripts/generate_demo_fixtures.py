# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

"""Generate fictional demo documents locally; no research or visitor data."""

from __future__ import annotations

from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "data" / "demo"
DESTINATION.mkdir(parents=True, exist_ok=True)


def make_table_image() -> Path:
    font_path = Path("C:/Windows/Fonts/meiryo.ttc")
    if not font_path.is_file():
        raise RuntimeError("Windows のメイリオフォントが見つかりません")
    title_font = ImageFont.truetype(str(font_path), 42)
    font = ImageFont.truetype(str(font_path), 34)
    small = ImageFont.truetype(str(font_path), 25)
    image = Image.new("RGB", (1200, 740), "white")
    draw = ImageDraw.Draw(image)
    draw.text((70, 54), "架空デモ資料：ルミナ水槽", font=title_font, fill="#15344c")
    draw.text((70, 125), "この表は実在の科学館の資料ではありません。", font=small, fill="#536775")
    xs, ys = [70, 460, 800, 1130], [205, 290, 385, 480, 575]
    for x in xs:
        draw.line([(x, ys[0]), (x, ys[-1])], fill="#6a7c87", width=3)
    for y in ys:
        draw.line([(xs[0], y), (xs[-1], y)], fill="#6a7c87", width=3)
    cells = [
        ("項目", "値", "単位"),
        ("水温", "17", "℃"),
        ("流量", "42", "L/分"),
        ("識別番号", "LM-482", "—"),
    ]
    for row_index, row in enumerate(cells):
        for column_index, value in enumerate(row):
            draw.text((xs[column_index] + 25, ys[row_index] + 18), value, font=font, fill="#172d3b")
    draw.text((70, 630), "数値・名称は実演のための架空設定です。", font=small, fill="#536775")
    path = DESTINATION / "fictional_lumina_table.png"
    image.save(path)
    return path


def make_text_pdf() -> Path:
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_font(fontname="meiryo", fontfile="C:/Windows/Fonts/meiryo.ttc")
    content = [
        "架空デモ資料：ソラノ光路",
        "この展示は実在の科学館の資料ではありません。",
        "体験時間は23分です。",
        "展示に使う光の色は青です。",
        "ルミナ水槽とは別の展示です。",
    ]
    for index, line in enumerate(content):
        page.insert_text((55, 90 + index * 60), line, fontsize=18, fontname="meiryo")
    path = DESTINATION / "fictional_sorano_guide.pdf"
    document.save(path)
    document.close()
    return path


if __name__ == "__main__":
    print(make_table_image())
    print(make_text_pdf())
