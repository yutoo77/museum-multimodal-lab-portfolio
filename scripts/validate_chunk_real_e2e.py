# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

"""Isolated, real-Ollama check of long-PDF chunking and source provenance.

Run with the repository's Python environment while the dedicated Ollama server
is available at 127.0.0.1:11436. Every run creates a new profile under
outputs/chunk-real-e2e; this script never opens the normal data directory.
The generated PDF and all its contents are fictional.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from museum_multimodal_lab.config import Settings
from museum_multimodal_lab.providers import FasterWhisperProvider, OllamaProvider
from museum_multimodal_lab.retrieval import rank_chunks
from museum_multimodal_lab.service import LabService

QUESTION = "暁星帯と海霧帯の安全札はそれぞれ何色ですか？"
OUT_OF_SCOPE_QUESTION = "この資料に記載された入館料金はいくらですか？"
BLUE_FACT = "暁星帯の安全札は青色"
WHITE_FACT = "海霧帯の安全札は白色"
WHITE_RAW = "海霧帯の安全札は白 色"
JOB_TIMEOUT_SECONDS = 360


def _make_pdf(path: Path) -> None:
    font = Path("C:/Windows/Fonts/meiryo.ttc")
    if not font.is_file():
        raise RuntimeError("Japanese PDF font is unavailable")
    lines = [
        "架空デモ資料：展示運用記録（実在の科学館資料ではありません）",
        BLUE_FACT + "です。",
        *(
            f"記録段落{index}：架空の点検通路と記録手順を順に確認しました。"
            for index in range(120)
        ),
        WHITE_RAW + "です。",
    ]
    line_height = 26
    last_baseline = 65 + (len(lines) - 1) * line_height
    page_height = last_baseline + 80
    pdf = pymupdf.open()
    try:
        page = pdf.new_page(width=840, height=page_height)
        page.insert_font(fontname="meiryo", fontfile=str(font))
        for index, line in enumerate(lines):
            page.insert_text((48, 65 + index * line_height), line, fontname="meiryo", fontsize=11)
        if last_baseline >= page.rect.height - 30:
            raise AssertionError("Fixture text would fall outside the PDF page")
        pdf.save(path)
    finally:
        pdf.close()

    # Confirm both visible source facts are inside the one page's text layer.
    with pymupdf.open(path) as check:
        if check.page_count != 1:
            raise AssertionError("Fixture PDF must have exactly one page")
        text = check[0].get_text("text", sort=True)
        if BLUE_FACT not in text or WHITE_RAW not in text or len(text) <= 2400:
            raise AssertionError("Fixture PDF did not preserve the two distant facts")


def _wait_for_job(service: LabService, job_id: str, label: str, report: dict[str, Any]) -> dict[str, Any]:
    start = time.monotonic()
    while time.monotonic() - start < JOB_TIMEOUT_SECONDS:
        state = service.job(job_id)
        if state["status"] in {"completed", "failed"}:
            report.setdefault("job_timings_sec", {})[label] = round(time.monotonic() - start, 2)
            report.setdefault("jobs", {})[label] = {"status": state["status"], "error": state.get("error")}
            if state["status"] != "completed":
                raise RuntimeError(f"{label} job failed: {state.get('error')}")
            return state
        time.sleep(0.5)
    raise TimeoutError(f"{label} job exceeded {JOB_TIMEOUT_SECONDS} seconds")


def _chunk_rows(service: LabService, document_id: str) -> list[dict[str, Any]]:
    with service.db.connect() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT c.id,c.document_id,c.unit_id,c.ordinal,c.search_text,c.citation_text,"
            "c.embedding_json,u.page_number FROM chunks c "
            "JOIN extracted_units u ON c.unit_id=u.id "
            "WHERE c.document_id=? ORDER BY c.ordinal,c.id", (document_id,)
        )]


def _fact_labels(text: str) -> list[str]:
    labels = []
    if BLUE_FACT in text:
        labels.append("blue")
    if WHITE_FACT in text:
        labels.append("white")
    return labels or ["filler"]


def _rank_summary(ranked: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "rank": position,
            "chunk_id": row["id"],
            "ordinal": row.get("ordinal"),
            "unit_id": row["unit_id"],
            "page_number": row.get("page_number"),
            "facts": _fact_labels(row["search_text"]),
            "rrf_score": round(row["rrf_score"], 6),
            "lexical_score": round(row["lexical_score"], 6),
            "dense_score": round(row["dense_score"], 6),
        }
        for position, row in enumerate(ranked, 1)
    ]


def _source_summary(answer: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": answer["status"],
        "answer": answer["answer"],
        "sources": [
            {
                "document_id": source["document_id"],
                "unit_id": source["unit_id"],
                "chunk_id": source["chunk_id"],
                "page_number": source["page_number"],
                "facts": _fact_labels(source["snippet"]),
                "original_url": source["original_url"],
            }
            for source in answer["sources"]
        ],
    }


def _sources_current(
    answer: dict[str, Any], rows: list[dict[str, Any]], document_id: str, unit_id: str, service: LabService
) -> bool:
    indexed = {row["id"]: row for row in rows}
    if not answer["sources"]:
        return False
    original, _, _ = service.original_path(document_id)  # checks stored file and SHA256
    if not original.is_file():
        return False
    return all(
        source["document_id"] == document_id
        and source["unit_id"] == unit_id
        and source["page_number"] == 1
        and source["original_url"] == f"/api/documents/{document_id}/original#page=1"
        and source["chunk_id"] in indexed
        and source["snippet"] == indexed[source["chunk_id"]]["citation_text"][:3000]
        for source in answer["sources"]
    )


def _record_current_ranking(
    query_vector: list[float], rows: list[dict[str, Any]], report: dict[str, Any]
) -> None:
    # A clean public release has no development-history baseline. Validate the
    # current real-embedding ranking without importing a prior Git revision.
    ranked = rank_chunks(QUESTION, query_vector, rows)
    report["current_index"] = {"chunk_count": len(rows), "ranked": _rank_summary(ranked)}


def _probe_guards(service: LabService, provider: OllamaProvider) -> dict[str, Any]:
    # This part intentionally injects invalid *fake answers* into the same
    # isolated service. Embeddings/retrieval remain real; LLM generation does not.
    original_answer = provider.answer
    try:
        provider.answer = lambda question, evidence: {  # type: ignore[method-assign]
            "answer": "安全札は9999色です。", "source_ids": [evidence[0]["id"]] if evidence else []
        }
        numeric = service.ask(QUESTION)
        provider.answer = lambda question, evidence: {  # type: ignore[method-assign]
            "answer": "安全札は青色です。", "source_ids": ["S999"]
        }
        forged = service.ask(QUESTION)
    finally:
        provider.answer = original_answer  # type: ignore[method-assign]
    return {
        "method": "real retrieval with deliberately fake invalid model outputs",
        "unsupported_number_status": numeric["status"],
        "forged_source_id_status": forged["status"],
    }


def main() -> int:
    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    run_dir = ROOT / "outputs" / "chunk-real-e2e" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    report_path = run_dir / "result.json"
    report: dict[str, Any] = {
        "run_id": run_id,
        "started_utc": datetime.now(UTC).isoformat(),
        "scope": "fresh isolated SQLite, self-made fictional one-page PDF, localhost Ollama",
        "status": "running",
        "checks": {},
    }
    provider: OllamaProvider | None = None
    service: LabService | None = None
    stage = "preflight"
    try:
        settings = Settings(data_dir=run_dir / "data", ollama_url="http://127.0.0.1:11436")
        provider = OllamaProvider(settings)
        installed = provider.models()
        required = {settings.embedding_model, settings.answer_model}
        if not required <= set(installed):
            raise RuntimeError("Required local embedding/answer model is not installed")
        report["models"] = {"embedding": settings.embedding_model, "answer": settings.answer_model}

        stage = "fictional_pdf"
        pdf_path = run_dir / "fictional_long_page.pdf"
        _make_pdf(pdf_path)
        report["fixture"] = {"page_count": 1, "size_bytes": pdf_path.stat().st_size, "fictional": True}

        stage = "upload_and_extract"
        service = LabService(settings, provider, FasterWhisperProvider(settings))
        document = service.upload(pdf_path.name, pdf_path.read_bytes())
        document_id = document["id"]
        report["document_id"] = document_id
        _wait_for_job(service, service.extract(document_id)["job_id"], "extract", report)
        units = service.document(document_id)["units"]
        if len(units) != 1 or units[0]["kind"] != "pdf_text" or units[0]["page_number"] != 1:
            raise AssertionError("Expected exactly one text Unit on PDF page 1")
        unit_id = units[0]["id"]
        raw_text = units[0]["extracted_text"]
        if BLUE_FACT not in raw_text or WHITE_RAW not in raw_text:
            raise AssertionError("Extracted PDF text lost a source fact")
        report["extraction"] = {"unit_count": 1, "kind": "pdf_text", "page_number": 1, "text_chars": len(raw_text)}

        stage = "human_test_correction"
        corrected_text = raw_text.replace(WHITE_RAW, WHITE_FACT, 1)
        if corrected_text == raw_text:
            raise AssertionError("The synthetic spacing correction was not applied")
        service.edit_unit(unit_id, corrected_text)
        detail = service.document(document_id)
        report["correction"] = {
            "actor": "test operator, not museum staff",
            "change": "white fact spacing only; value unchanged",
            "review_event_count": len(detail["review_history"]),
        }

        stage = "held_before_approval"
        before_approval = service.ask(QUESTION)
        report["before_approval"] = _source_summary(before_approval)
        report["checks"]["held_before_approval"] = before_approval["status"] == "held" and not before_approval["sources"]

        stage = "approve"
        _wait_for_job(service, service.approve(document_id)["job_id"], "approve", report)
        rows = _chunk_rows(service, document_id)
        report["checks"]["approved_chunks_share_correct_unit_and_page"] = (
            len(rows) >= 2 and all(row["unit_id"] == unit_id and row["page_number"] == 1 for row in rows)
        )

        stage = "real_embedding_rank"
        query_vector = provider.embed(QUESTION)
        _record_current_ranking(query_vector, rows, report)
        new_facts = {fact for row in report["current_index"]["ranked"] for fact in row["facts"]}
        report["checks"]["both_distant_facts_ranked"] = {"blue", "white"} <= new_facts
        report["checks"]["no_filler_in_top_two"] = all(
            "filler" not in row["facts"] for row in report["current_index"]["ranked"][:2]
        )

        stage = "real_answer"
        started = time.monotonic()
        answer = service.ask(QUESTION)
        report["answer_seconds"] = round(time.monotonic() - started, 2)
        report["real_answer"] = _source_summary(answer)
        report["checks"]["answer_cites_current_original"] = _sources_current(answer, rows, document_id, unit_id, service)
        answer_facts = {fact for source in report["real_answer"]["sources"] for fact in source["facts"]}
        report["checks"]["answer_cites_both_facts"] = {"blue", "white"} <= answer_facts
        report["checks"]["answer_mentions_both_colors"] = (
            answer["status"] == "answered" and "青" in answer["answer"] and "白" in answer["answer"]
        )

        stage = "real_out_of_scope_hold"
        outside = service.ask(OUT_OF_SCOPE_QUESTION)
        report["real_out_of_scope"] = _source_summary(outside)
        report["checks"]["out_of_scope_held"] = outside["status"] == "held" and not outside["sources"]

        stage = "forged_answer_guard_probes"
        report["guard_probes"] = _probe_guards(service, provider)
        report["checks"]["numeric_and_source_id_guards"] = (
            report["guard_probes"]["unsupported_number_status"] == "held"
            and report["guard_probes"]["forged_source_id_status"] == "held"
        )

        stage = "reindex"
        old_chunk_ids = {row["id"] for row in rows}
        _wait_for_job(service, service.reindex(document_id)["job_id"], "reindex", report)
        reindexed_rows = _chunk_rows(service, document_id)
        report["reindex"] = {
            "old_chunk_count": len(rows),
            "new_chunk_count": len(reindexed_rows),
            "all_ids_replaced": old_chunk_ids.isdisjoint({row["id"] for row in reindexed_rows}),
            "same_reviewed_text": [row["search_text"] for row in rows] == [row["search_text"] for row in reindexed_rows],
        }
        report["checks"]["reindex_replaces_chunks_safely"] = (
            report["reindex"]["all_ids_replaced"]
            and report["reindex"]["same_reviewed_text"]
            and all(row["unit_id"] == unit_id and row["page_number"] == 1 for row in reindexed_rows)
        )

        stage = "real_answer_after_reindex"
        started = time.monotonic()
        after = service.ask(QUESTION)
        report["answer_after_reindex_seconds"] = round(time.monotonic() - started, 2)
        report["real_answer_after_reindex"] = _source_summary(after)
        report["checks"]["post_reindex_source_current"] = _sources_current(
            after, reindexed_rows, document_id, unit_id, service
        ) and all(source["chunk_id"] not in old_chunk_ids for source in after["sources"])

        report["status"] = "passed" if all(report["checks"].values()) else "partial"
        return 0 if report["status"] == "passed" else 2
    except Exception as exc:  # noqa: BLE001 - persist the failed stage and artifacts
        report["status"] = "failed"
        report["failure_stage"] = stage
        report["error_type"] = type(exc).__name__
        report["error"] = str(exc).replace(str(run_dir), "[isolated-output]")
        return 1
    finally:
        if service is not None:
            service.close()
        if provider is not None:
            provider.client.close()
        report["finished_utc"] = datetime.now(UTC).isoformat()
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{report['status']}: {report_path}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
