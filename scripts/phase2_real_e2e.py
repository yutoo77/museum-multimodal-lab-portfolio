# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

"""Run a local-only, real-model smoke against a disposable Phase 2 data profile.

The fixed values below are assertions for self-made fictional fixtures, never app answers.
This script intentionally refuses to run against the normal demo backend (port 8765).
"""

from __future__ import annotations

import argparse
import io
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "outputs" / "phase2-e2e" / datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
FIXTURES = ROOT / "data" / "demo"


def api_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme != "http" or parts.hostname != "127.0.0.1" or parts.port != 8766 or parts.path not in {"", "/"}:
        raise ValueError("Phase 2 の隔離Backend http://127.0.0.1:8766 のみを指定してください")
    return value.rstrip("/")


def call(client: httpx.Client, method: str, path: str, **kwargs: object) -> dict:
    response = client.request(method, path, **kwargs)
    response.raise_for_status()
    return response.json()


def job(client: httpx.Client, document_id: str, action: str, record: dict) -> None:
    started = time.monotonic()
    accepted = call(client, "POST", f"/api/documents/{document_id}/{action}")
    job_id = accepted["job_id"]
    while time.monotonic() - started < 360:
        state = call(client, "GET", f"/api/jobs/{job_id}")
        if state["status"] in {"completed", "failed"}:
            record[f"{action}_seconds"] = round(time.monotonic() - started, 2)
            record[f"{action}_job"] = state
            if state["status"] != "completed":
                raise AssertionError(f"{action} failed: {state['error']}")
            print(f"{action}: {record[f'{action}_seconds']}s", flush=True)
            return
        time.sleep(1)
    raise TimeoutError(f"{action} job did not finish within 360 seconds")


def upload(client: httpx.Client, path: Path, mime: str) -> dict:
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open("rb") as source:
        return call(client, "POST", "/api/documents", files={"file": (path.name, source, mime)})


def edit_all(client: httpx.Client, document_id: str, corrected: str, record: dict) -> None:
    detail = call(client, "GET", f"/api/documents/{document_id}")
    units = detail["units"]
    if not units:
        raise AssertionError("No extracted units")
    record["raw_units"] = [
        {
            "kind": unit["kind"],
            "page_number": unit["page_number"],
            "start_sec": unit["start_sec"],
            "end_sec": unit["end_sec"],
            "raw_excerpt": unit["extracted_text"][:300],
        }
        for unit in units
    ]
    for index, unit in enumerate(units):
        call(client, "PATCH", f"/api/extracted-units/{unit['id']}", json={"corrected_text": corrected if index == 0 else ""})
    record["corrected_text"] = corrected
    record["correction_actor"] = "test operator; not museum staff"


def ask(client: httpx.Client, question: str, expected: str, document_id: str, record: dict) -> dict:
    started = time.monotonic()
    response = call(client, "POST", "/api/ask", json={"question": question})
    record["question"] = question
    record["ask_seconds"] = round(time.monotonic() - started, 2)
    record["answer"] = response
    if response["status"] != "answered" or expected not in response["answer"]:
        raise AssertionError(f"Unexpected answer for {question}: {response}")
    if not response["sources"] or any(source["document_id"] != document_id for source in response["sources"]):
        raise AssertionError(f"Wrong source for {question}: {response['sources']}")
    for source in response["sources"]:
        original = client.get(source["original_url"].split("#")[0])
        if original.status_code != 200 or not original.content:
            raise AssertionError(f"Source original unavailable: {source['original_url']}")
    print(f"ask: {question} -> {response['answer']} ({record['ask_seconds']}s)", flush=True)
    return response


def make_fictional_pdf(path: Path, identifier: str, temperature: int, flow: int) -> None:
    font = Path("C:/Windows/Fonts/meiryo.ttc")
    if not font.is_file():
        raise FileNotFoundError(font)
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_font(fontname="meiryo", fontfile=str(font))
    lines = [
        "架空デモ資料：ルミナ水槽の観測表",
        "これは実在の科学館資料ではありません。",
        f"識別番号は {identifier} です。",
        f"水温は {temperature}℃ です。",
        f"流量は {flow} L/分 です。",
    ]
    for index, line in enumerate(lines):
        page.insert_text((55, 90 + index * 60), line, fontsize=18, fontname="meiryo")
    document.save(path)
    document.close()


def make_image_only_pdf(path: Path) -> None:
    font = Path("C:/Windows/Fonts/meiryo.ttc")
    if not font.is_file():
        raise FileNotFoundError(font)
    image = Image.new("RGB", (1200, 720), "white")
    draw = ImageDraw.Draw(image)
    heading = ImageFont.truetype(str(font), 42)
    body = ImageFont.truetype(str(font), 36)
    draw.text((65, 55), "架空デモ資料：ノヴァ水槽", font=heading, fill="#15344c")
    draw.text((65, 150), "識別番号 NV-918", font=body, fill="#172d3b")
    draw.text((65, 245), "水温 26 ℃", font=body, fill="#172d3b")
    draw.text((65, 340), "流量 51 L/分", font=body, fill="#172d3b")
    draw.text((65, 480), "実在の科学館の資料ではありません。", font=body, fill="#536775")
    raster = io.BytesIO()
    image.save(raster, format="PNG")
    document = fitz.open()
    page = document.new_page(width=600, height=360)
    page.insert_image(page.rect, stream=raster.getvalue())
    document.save(path)
    document.close()


def run(client: httpx.Client, result: dict) -> None:
    health = call(client, "GET", "/api/health")
    if health["documents_count"] != 0 or not health["ollama"]["available"]:
        raise AssertionError("Isolated data profile must start empty with Ollama available")
    result["models"] = health["models"]

    image = {"fixture": "fictional_lumina_table.png"}
    result["image"] = image
    image_doc = upload(client, FIXTURES / image["fixture"], "image/png")
    image["document_id"] = image_doc["id"]
    job(client, image_doc["id"], "extract", image)
    held = call(client, "POST", "/api/ask", json={"question": "LM-482 の水温は何℃ですか？"})
    if held["status"] != "held":
        raise AssertionError("Unapproved image was searchable")
    edit_all(client, image_doc["id"], "架空展示ルミナ水槽。識別番号 LM-482。水温は17℃、流量は42 L/分です。", image)
    job(client, image_doc["id"], "approve", image)
    ask(client, "LM-482 の水温は何℃ですか？", "17", image_doc["id"], image)

    pdf = {"fixture": "fictional_sorano_guide.pdf"}
    result["text_pdf"] = pdf
    pdf_doc = upload(client, FIXTURES / pdf["fixture"], "application/pdf")
    pdf["document_id"] = pdf_doc["id"]
    job(client, pdf_doc["id"], "extract", pdf)
    edit_all(client, pdf_doc["id"], "架空展示ソラノ光路。体験時間は23分です。展示に使う光の色は青です。ルミナ水槽とは別の展示です。", pdf)
    job(client, pdf_doc["id"], "approve", pdf)
    answer = ask(client, "ソラノ光路の体験時間は何分ですか？", "23", pdf_doc["id"], pdf)
    if answer["sources"][0]["page_number"] != 1:
        raise AssertionError("PDF page provenance missing")

    visual_pdf_path = OUTPUT / "fixtures" / "fictional_nova_image_only.pdf"
    visual_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    make_image_only_pdf(visual_pdf_path)
    visual_pdf = {"fixture": visual_pdf_path.name}
    result["image_only_pdf"] = visual_pdf
    visual_doc = upload(client, visual_pdf_path, "application/pdf")
    visual_pdf["document_id"] = visual_doc["id"]
    job(client, visual_doc["id"], "extract", visual_pdf)
    edit_all(client, visual_doc["id"], "架空展示ノヴァ水槽。識別番号 NV-918。水温は26℃、流量は51 L/分です。", visual_pdf)
    job(client, visual_doc["id"], "approve", visual_pdf)
    visual_answer = ask(client, "識別番号 NV-918 の水温は何℃ですか？", "26", visual_doc["id"], visual_pdf)
    if visual_answer["sources"][0]["page_number"] != 1:
        raise AssertionError("Image-only PDF page provenance missing")

    audio = {"fixture": "fictional_stardust_announcement.wav"}
    result["wav"] = audio
    audio_doc = upload(client, FIXTURES / audio["fixture"], "audio/wav")
    audio["document_id"] = audio_doc["id"]
    job(client, audio_doc["id"], "extract", audio)
    edit_all(client, audio_doc["id"], "架空の展示、星くずドームの最終案内は午後3時40分です。", audio)
    job(client, audio_doc["id"], "approve", audio)
    answer = ask(client, "星くずドームの最終案内は何時何分ですか？", "3時40分", audio_doc["id"], audio)
    if answer["sources"][0]["start_sec"] is None:
        raise AssertionError("Audio timestamp provenance missing")

    variant_dir = OUTPUT / "fixtures"
    variant_dir.mkdir(parents=True, exist_ok=True)
    for identifier, temperature, flow in (("LM-483", 19, 44), ("LM-484", 21, 46)):
        path = variant_dir / f"fictional_lumina_{identifier.lower()}.pdf"
        make_fictional_pdf(path, identifier, temperature, flow)
        item = {"fixture": path.name, "expected_temperature": temperature}
        result[identifier] = item
        doc = upload(client, path, "application/pdf")
        item["document_id"] = doc["id"]
        job(client, doc["id"], "extract", item)
        edit_all(client, doc["id"], f"架空展示ルミナ水槽。識別番号 {identifier}。水温は{temperature}℃、流量は{flow} L/分です。", item)
        job(client, doc["id"], "approve", item)
    for identifier, temperature in (("LM-482", 17), ("LM-483", 19), ("LM-484", 21)):
        item = result["image"] if identifier == "LM-482" else result[identifier]
        ask(client, f"識別番号 {identifier} の水温は何℃ですか？", str(temperature), item["document_id"], item)

    out_of_scope = call(client, "POST", "/api/ask", json={"question": "名古屋市科学館の開館時間は何時ですか？"})
    result["out_of_scope"] = out_of_scope
    if out_of_scope["status"] != "held":
        raise AssertionError("Out-of-scope question was not held")

    deleted_id = result["LM-483"]["document_id"]
    response = client.delete(f"/api/documents/{deleted_id}")
    response.raise_for_status()
    result["deleted_id"] = deleted_id
    after_delete = call(client, "POST", "/api/ask", json={"question": "識別番号 LM-483 の水温は何℃ですか？"})
    result["after_delete"] = after_delete
    if after_delete["status"] != "held":
        raise AssertionError("Deleted source remained searchable")

    replacement_dir = OUTPUT / "fixtures" / "replacement"
    replacement_dir.mkdir(parents=True, exist_ok=True)
    newest = replacement_dir / "fictional_lumina_lm-484.pdf"
    make_fictional_pdf(newest, "LM-484", 22, 48)
    replacement = upload(client, newest, "application/pdf")
    result["replacement"] = {"document_id": replacement["id"], "version": replacement["version"]}
    if replacement["version"] != 2:
        raise AssertionError("Replacement version did not increment")
    before_new_approval = call(client, "POST", "/api/ask", json={"question": "識別番号 LM-484 の水温は何℃ですか？"})
    if before_new_approval["status"] != "held":
        raise AssertionError("Old LM-484 index remained active after replacement")
    job(client, replacement["id"], "extract", result["replacement"])
    edit_all(client, replacement["id"], "架空展示ルミナ水槽。識別番号 LM-484。水温は22℃、流量は48 L/分です。", result["replacement"])
    job(client, replacement["id"], "approve", result["replacement"])
    ask(client, "識別番号 LM-484 の水温は何℃ですか？", "22", replacement["id"], result["replacement"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", type=api_url, default="http://127.0.0.1:8766")
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    result: dict = {"started_utc": datetime.now(UTC).isoformat(), "status": "running", "api": args.api}
    try:
        with httpx.Client(base_url=args.api, timeout=150, trust_env=False, follow_redirects=False) as client:
            run(client, result)
        result["status"] = "passed"
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        result["finished_utc"] = datetime.now(UTC).isoformat()
        (OUTPUT / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"result: {OUTPUT / 'result.json'} ({result['status']})", flush=True)


if __name__ == "__main__":
    main()
