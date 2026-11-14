# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database
from .extraction import ExtractionError, extract_document, validate_upload
from .providers import AsrProvider, ModelError, ModelProvider
from .retrieval import chunk_text, clarification_candidates, rank_chunks


class NotFoundError(ValueError):
    pass


class ConflictError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _uuid() -> str:
    return str(uuid.uuid4())


def _source_card(row: dict[str, Any], snippet: str) -> dict[str, Any]:
    fragment = (
        f"#page={row['page_number']}" if row["page_number"] else
        f"#t={row['start_sec']:.2f}" if row["start_sec"] is not None else ""
    )
    return {
        "chunk_id": row["id"],
        "document_id": row["document_id"],
        "document_name": row["document_name"],
        "unit_id": row["unit_id"],
        "kind": row["kind"],
        "page_number": row["page_number"],
        "start_sec": row["start_sec"],
        "end_sec": row["end_sec"],
        "snippet": snippet,
        "original_url": f"/api/documents/{row['document_id']}/original{fragment}",
    }


class LabService:
    def __init__(self, settings: Settings, models: ModelProvider, asr: AsrProvider):
        self.settings = settings
        self.models = models
        self.asr = asr
        self.originals = settings.data_dir / "originals"
        self.originals.mkdir(parents=True, exist_ok=True)
        self.db = Database(settings.data_dir / "museum_multimodal_lab.sqlite3")
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mml-job")
        self._submit_lock = threading.Lock()
        self._model_lock = threading.Lock()

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)

    def health(self) -> dict[str, Any]:
        try:
            installed = self.models.models()
            available = True
        except ModelError:
            installed, available = [], False
        with self.db.connect() as connection:
            count = connection.execute("SELECT count(*) FROM documents WHERE status != 'superseded'").fetchone()[0]
        return {
            "status": "ok",
            "ollama": {"available": available, "models": installed},
            "models": {
                "answer": self.settings.answer_model,
                "embedding": self.settings.embedding_model,
                "vision": self.settings.vision_model,
                "asr": f"faster-whisper:{self.settings.asr_model}" if self.asr.available() else "未導入",
            },
            "documents_count": count,
        }

    def _document_row(self, connection: sqlite3.Connection, document_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        if row is None:
            raise NotFoundError("資料が見つかりません")
        return row

    def _stored_path(self, stored_name: str) -> Path:
        path = (self.originals / stored_name).resolve()
        if path.parent != self.originals.resolve():
            raise ConflictError("保存先が不正です")
        return path

    def _original_matches(self, row: sqlite3.Row | dict[str, Any]) -> bool:
        try:
            path = self._stored_path(row["stored_name"])
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
            return digest.hexdigest() == row["sha256"]
        except (OSError, ConflictError):
            return False

    def _document(self, connection: sqlite3.Connection, row: sqlite3.Row, *, include_history: bool = False) -> dict[str, Any]:
        document = dict(row)
        document.pop("stored_name", None)
        units = connection.execute("SELECT * FROM extracted_units WHERE document_id=? ORDER BY page_number, start_sec, created_at, id", (row["id"],)).fetchall()
        document["units"] = [dict(unit) for unit in units]
        document["unit_count"] = len(units)
        document["original_available"] = self._original_matches(row)
        document["page_count"] = None
        if row["mime_type"] == "application/pdf" and document["original_available"]:
            try:
                import pymupdf

                with pymupdf.open(self._stored_path(row["stored_name"])) as pdf:
                    document["page_count"] = pdf.page_count
            except Exception:  # noqa: BLE001 - corrupted/missing originals remain visible for diagnosis
                document["original_available"] = False
        chunks = connection.execute("SELECT embedding_model, embedding_json, embedding_dim FROM chunks WHERE document_id=?", (row["id"],)).fetchall()
        dimensions: set[int] = set()
        invalid_vector = False
        for chunk in chunks:
            try:
                vector = json.loads(chunk["embedding_json"])
                if not isinstance(vector, list) or not vector or len(vector) != chunk["embedding_dim"] or any(
                    not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector
                ):
                    invalid_vector = True
                else:
                    dimensions.add(len(vector))
            except (TypeError, ValueError):
                invalid_vector = True
        document["reindex_required"] = row["status"] == "indexed" and (
            not chunks or invalid_vector or len(dimensions) != 1 or any(chunk["embedding_model"] != self.settings.embedding_model for chunk in chunks)
        )
        document["review_count"] = connection.execute("SELECT count(*) FROM review_events WHERE document_id=?", (row["id"],)).fetchone()[0]
        if include_history:
            document["review_history"] = [dict(event) for event in connection.execute(
                "SELECT * FROM review_events WHERE document_id=? ORDER BY created_at, rowid", (row["id"],)
            )]
        return document

    def list_documents(self) -> dict[str, list[dict[str, Any]]]:
        with self.db.connect() as connection:
            rows = connection.execute("SELECT * FROM documents ORDER BY created_at DESC, version DESC, id DESC").fetchall()
            return {"documents": [self._document(connection, row) for row in rows]}

    def document(self, document_id: str) -> dict[str, Any]:
        with self.db.connect() as connection:
            return self._document(connection, self._document_row(connection, document_id), include_history=True)

    def original_path(self, document_id: str) -> tuple[Path, str, str]:
        with self.db.connect() as connection:
            row = self._document_row(connection, document_id)
            path = self._stored_path(row["stored_name"])
            if not path.is_file():
                raise NotFoundError("原本が見つかりません")
            if not self._original_matches(row):
                raise ConflictError("原本が登録時から変更されています")
            return path, row["mime_type"], row["name"]

    def upload(self, display_name: str, data: bytes) -> dict[str, Any]:
        name = display_name.strip()
        if not name or len(name) > 200 or any(char in name for char in "/\\\x00\r\n") or name in {".", ".."}:
            raise ExtractionError("ファイル名が不正です")
        mime_type, extension = validate_upload(name, data, self.settings)
        digest = hashlib.sha256(data).hexdigest()
        document_id = _uuid()
        stored_name = f"{document_id}{extension}"
        path = self.originals / stored_name
        now = utc_now()
        try:
            with self.db.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                latest = connection.execute(
                    "SELECT * FROM documents WHERE name=? AND status!='superseded' ORDER BY version DESC LIMIT 1", (name,)
                ).fetchone()
                if latest is not None and latest["sha256"] == digest and self._original_matches(latest):
                    return self._document(connection, latest)
                previous = connection.execute("SELECT COALESCE(MAX(version),0) FROM documents WHERE name=?", (name,)).fetchone()[0]
                # A newly supplied version immediately invalidates the old index.
                old_ids = [row[0] for row in connection.execute("SELECT id FROM documents WHERE name=? AND status != 'superseded'", (name,))]
                if old_ids:
                    placeholders = ",".join("?" for _ in old_ids)
                    active = connection.execute(
                        f"SELECT 1 FROM jobs WHERE document_id IN ({placeholders}) AND status IN ('pending','running') LIMIT 1",
                        old_ids,
                    ).fetchone()
                    if active:
                        raise ConflictError("同名資料の処理が終わってから差し替えてください")
                with path.open("xb") as output:
                    output.write(data)
                for old_id in old_ids:
                    connection.execute("DELETE FROM chunks WHERE document_id=?", (old_id,))
                    connection.execute("UPDATE documents SET status='superseded', updated_at=? WHERE id=?", (now, old_id))
                connection.execute(
                    "INSERT INTO documents(id,name,mime_type,stored_name,sha256,version,status,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (document_id, name, mime_type, stored_name, digest, previous + 1, "draft", now, now),
                )
                return self._document(connection, self._document_row(connection, document_id))
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def _create_job(self, document_id: str, kind: str, *, reindex: bool = False) -> str:
        job_id, now = _uuid(), utc_now()
        with self._submit_lock, self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._document_row(connection, document_id)
            if row["status"] == "superseded":
                raise ConflictError("古い版の資料は処理できません")
            if not self._original_matches(row):
                raise ConflictError("原本が見つからないか変更されています。登録し直してください")
            active = connection.execute("SELECT 1 FROM jobs WHERE document_id=? AND status IN ('pending','running')", (document_id,)).fetchone()
            if active:
                raise ConflictError("この資料は処理中です")
            if kind == "approve":
                count = connection.execute("SELECT count(*) FROM extracted_units WHERE document_id=?", (document_id,)).fetchone()[0]
                allowed_status = {"indexed"} if reindex else {"draft", "reviewed"}
                if not count or row["status"] not in allowed_status:
                    raise ConflictError("抽出結果を確認してから登録してください")
                if not reindex:
                    connection.execute(
                        "INSERT INTO review_events(id,document_id,unit_id,action,previous_text,new_text,created_at) "
                        "VALUES(?,?,NULL,'approved',NULL,NULL,?)", (_uuid(), document_id, now)
                    )
                    connection.execute("UPDATE documents SET status='reviewed', error=NULL, updated_at=? WHERE id=?", (now, document_id))
                else:
                    # Keep the current index usable until the replacement embeddings exist.
                    connection.execute("UPDATE documents SET error=NULL, updated_at=? WHERE id=?", (now, document_id))
            else:
                # Re-extraction is a staged replacement. A model failure must not
                # erase previously reviewed text or its still-valid index.
                connection.execute("UPDATE documents SET error=NULL, updated_at=? WHERE id=?", (now, document_id))
            connection.execute(
                "INSERT INTO jobs(id,document_id,kind,status,progress,created_at,updated_at) VALUES(?,?,?,'pending',0,?,?)",
                (job_id, document_id, kind, now, now),
            )
        try:
            self.executor.submit(self._run_job, job_id, document_id, kind)
        except RuntimeError as exc:
            error = "処理を開始できませんでした。アプリを再起動してください"
            self._update_job(job_id, "failed", 0, error)
            with self.db.connect() as connection:
                connection.execute("UPDATE documents SET error=?, updated_at=? WHERE id=?", (error, utc_now(), document_id))
            raise ConflictError(error) from exc
        return job_id

    def extract(self, document_id: str) -> dict[str, str]:
        return {"job_id": self._create_job(document_id, "extract")}

    def approve(self, document_id: str) -> dict[str, str]:
        return {"job_id": self._create_job(document_id, "approve")}

    def reindex(self, document_id: str) -> dict[str, str]:
        return {"job_id": self._create_job(document_id, "approve", reindex=True)}

    def job(self, job_id: str) -> dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise NotFoundError("処理が見つかりません")
            result = dict(row)
            result["phase"] = (
                "受付済み" if row["status"] == "pending" else
                "資料を読み取り中" if row["status"] == "running" and row["kind"] == "extract" else
                "知識を登録中" if row["status"] == "running" else
                "内容の確認待ち" if row["status"] == "completed" and row["kind"] == "extract" else
                "登録完了" if row["status"] == "completed" else "失敗"
            )
            if row["status"] == "running" and row["started_at"]:
                result["elapsed_sec"] = max(0, (datetime.now(UTC) - datetime.fromisoformat(row["started_at"])).total_seconds())
            if row["started_at"] and row["finished_at"]:
                started = datetime.fromisoformat(row["started_at"])
                finished = datetime.fromisoformat(row["finished_at"])
                if started.tzinfo is None and finished.tzinfo is not None:
                    started = started.replace(tzinfo=UTC)
                if started.tzinfo is not None and finished.tzinfo is None:
                    finished = finished.replace(tzinfo=UTC)
                result["duration_sec"] = (
                    finished - started
                ).total_seconds()
            return result

    def _update_job(self, job_id: str, status: str, progress: float, error: str | None = None) -> None:
        now = utc_now()
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE jobs SET status=?, progress=?, error=?, updated_at=?, "
                "started_at=CASE WHEN ?='running' AND started_at IS NULL THEN ? ELSE started_at END, "
                "finished_at=CASE WHEN ? IN ('completed','failed') THEN ? ELSE finished_at END WHERE id=?",
                (status, progress, error, now, status, now, status, now, job_id),
            )

    def _run_job(self, job_id: str, document_id: str, kind: str) -> None:
        self._update_job(job_id, "running", 0)
        try:
            if kind == "extract":
                self._run_extract(job_id, document_id)
            else:
                self._run_approve(job_id, document_id)
        except (ExtractionError, ModelError, ConflictError, NotFoundError) as exc:
            error = str(exc)
            self._record_job_failure(document_id, kind, error)
            self._update_job(job_id, "failed", 0, error)
        except Exception:  # noqa: BLE001 - a background worker must persist unexpected failures
            error = "予期しない処理エラーです。今回の処理結果は反映されていません"
            self._record_job_failure(document_id, kind, error)
            self._update_job(job_id, "failed", 0, error)

    def _record_job_failure(self, document_id: str, kind: str, error: str) -> None:
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._document_row(connection, document_id)
            if kind == "extract":
                count = connection.execute("SELECT count(*) FROM extracted_units WHERE document_id=?", (document_id,)).fetchone()[0]
                status = row["status"] if count else "failed"
            else:
                # Initial approval was already marked reviewed; reindex keeps indexed.
                status = row["status"]
            connection.execute(
                "UPDATE documents SET status=?, error=?, updated_at=? WHERE id=?",
                (status, error, utc_now(), document_id),
            )

    def _run_extract(self, job_id: str, document_id: str) -> None:
        path, mime_type, _ = self.original_path(document_id)
        with self._model_lock:
            units, model_name, warning = extract_document(path, mime_type, self.settings, self.models, self.asr)
        usable_units = [unit for unit in units if str(unit.get("extracted_text", "")).strip()]
        if not usable_units:
            raise ExtractionError("抽出結果が空です")
        now = utc_now()
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._document_row(connection, document_id)
            if row["status"] == "superseded" or not self._original_matches(row):
                raise ConflictError("資料が処理中に変更されました")
            connection.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
            connection.execute("DELETE FROM extracted_units WHERE document_id=?", (document_id,))
            for unit in usable_units:
                connection.execute(
                    "INSERT INTO extracted_units(id,document_id,kind,extracted_text,corrected_text,page_number,bbox_json,"
                    "start_sec,end_sec,model_name,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,'draft',?,?)",
                    (
                        _uuid(), document_id, unit["kind"], unit["extracted_text"].strip(), None,
                        unit.get("page_number"), unit.get("bbox_json"), unit.get("start_sec"),
                        unit.get("end_sec"), unit.get("model_name"), now, now,
                    ),
                )
            connection.execute(
                "UPDATE documents SET status='draft', extraction_model=?, warning=?, error=NULL, updated_at=? WHERE id=?",
                (model_name, warning, now, document_id),
            )
        self._update_job(job_id, "completed", 1.0)

    def edit_unit(self, unit_id: str, corrected_text: str) -> dict[str, Any]:
        if len(corrected_text) > 20_000:
            raise ExtractionError("修正テキストが長すぎます")
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM extracted_units WHERE id=?", (unit_id,)).fetchone()
            if row is None:
                raise NotFoundError("抽出結果が見つかりません")
            document = self._document_row(connection, row["document_id"])
            if document["status"] in {"superseded", "failed"}:
                raise ConflictError("この資料は修正できません")
            active = connection.execute("SELECT 1 FROM jobs WHERE document_id=? AND status IN ('pending','running')", (row["document_id"],)).fetchone()
            if active:
                raise ConflictError("処理中の資料は修正できません")
            now = utc_now()
            before = row["corrected_text"] if row["corrected_text"] is not None else row["extracted_text"]
            after = corrected_text.strip()
            connection.execute(
                "INSERT INTO review_events(id,document_id,unit_id,action,previous_text,new_text,created_at) "
                "VALUES(?,?,?,'edited',?,?,?)", (_uuid(), row["document_id"], unit_id, before, after, now)
            )
            connection.execute("UPDATE extracted_units SET corrected_text=?, status='draft', updated_at=? WHERE id=?", (after, now, unit_id))
            connection.execute("DELETE FROM chunks WHERE document_id=?", (row["document_id"],))
            connection.execute("UPDATE documents SET status='draft', updated_at=? WHERE id=?", (now, row["document_id"]))
            return dict(connection.execute("SELECT * FROM extracted_units WHERE id=?", (unit_id,)).fetchone())

    def _run_approve(self, job_id: str, document_id: str) -> None:
        with self.db.connect() as connection:
            row = self._document_row(connection, document_id)
            if row["status"] not in {"reviewed", "indexed"}:
                raise ConflictError("資料が登録前に変更されました")
            starting_status = row["status"]
            revision = row["updated_at"]
            units = [dict(unit) for unit in connection.execute("SELECT * FROM extracted_units WHERE document_id=? ORDER BY created_at, id", (document_id,))]
        staged: list[tuple[str, str, int, str, list[float]]] = []
        with self._model_lock:
            for unit in units:
                content = unit["corrected_text"] if unit["corrected_text"] is not None else unit["extracted_text"]
                for ordinal, piece in enumerate(chunk_text(content, unit["kind"])):
                    if len(piece) > 8000:
                        raise ExtractionError("抽出結果が長すぎます。分割して登録してください")
                    vector = self.models.embed(piece)
                    staged.append((_uuid(), unit["id"], ordinal, piece, vector))
        if not staged:
            raise ExtractionError("登録できる確認済みテキストがありません")
        now, index_version = utc_now(), _uuid()
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._document_row(connection, document_id)
            if current["status"] != starting_status or current["updated_at"] != revision or not self._original_matches(current):
                raise ConflictError("登録中に資料が変更されました。もう一度確認してください")
            connection.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
            for chunk_id, unit_id, ordinal, content, vector in staged:
                connection.execute(
                    "INSERT INTO chunks(id,document_id,unit_id,search_text,citation_text,embedding_json,"
                    "embedding_model,embedding_dim,index_version,ordinal,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (chunk_id, document_id, unit_id, content, content, json.dumps(vector), self.settings.embedding_model, len(vector), index_version, ordinal, now),
                )
            connection.execute("UPDATE extracted_units SET status='reviewed', updated_at=? WHERE document_id=?", (now, document_id))
            connection.execute("UPDATE documents SET status='indexed', error=NULL, updated_at=? WHERE id=?", (now, document_id))
        self._update_job(job_id, "completed", 1.0)

    def delete(self, document_id: str) -> None:
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._document_row(connection, document_id)
            active = connection.execute("SELECT 1 FROM jobs WHERE document_id=? AND status IN ('pending','running')", (document_id,)).fetchone()
            if active:
                raise ConflictError("処理中の資料は削除できません")
            path = self._stored_path(row["stored_name"])
            # First invalidate the index. If file removal fails, keep a visible,
            # retryable record rather than silently orphaning the original.
            connection.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
            connection.execute(
                "UPDATE documents SET status='superseded', error='原本を削除中です', updated_at=? WHERE id=?",
                (utc_now(), document_id),
            )
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            with self.db.connect() as connection:
                connection.execute(
                    "UPDATE documents SET error='原本を削除できませんでした。再試行してください', updated_at=? WHERE id=?",
                    (utc_now(), document_id),
                )
            raise ConflictError("原本を削除できませんでした。再試行してください") from exc
        with self.db.connect() as connection:
            connection.execute("DELETE FROM documents WHERE id=?", (document_id,))

    def _cited_chunks_are_current(self, cited_rows: list[dict[str, Any]]) -> bool:
        """Reject a draft whose evidence changed during model generation."""
        with self.db.connect() as connection:
            for cited in cited_rows:
                current = connection.execute(
                    "SELECT c.index_version, c.document_id, c.unit_id, c.citation_text, c.embedding_json, c.embedding_dim, c.ordinal, "
                    "c.embedding_model, d.status AS document_status, d.stored_name, d.sha256, u.status AS unit_status "
                    "FROM chunks c JOIN documents d ON c.document_id=d.id "
                    "JOIN extracted_units u ON c.unit_id=u.id WHERE c.id=?",
                    (cited["id"],),
                ).fetchone()
                if (
                    current is None
                    or current["index_version"] != cited["index_version"]
                    or current["document_id"] != cited["document_id"]
                    or current["unit_id"] != cited["unit_id"]
                    or current["citation_text"] != cited["citation_text"]
                    or current["embedding_json"] != cited["embedding_json"]
                    or current["embedding_dim"] != cited["embedding_dim"]
                    or current["ordinal"] != cited["ordinal"]
                    or current["embedding_model"] != self.settings.embedding_model
                    or current["document_status"] != "indexed"
                    or current["unit_status"] != "reviewed"
                    or not self._original_matches(current)
                ):
                    return False
        return True

    def ask(self, question: str) -> dict[str, Any]:
        cleaned = question.strip()
        if not cleaned or len(cleaned) > 1500:
            raise ExtractionError("質問は1〜1500文字で入力してください")
        with self.db.connect() as connection:
            rows = [dict(row) for row in connection.execute(
                "SELECT c.*, d.name AS document_name, d.version, d.mime_type, u.kind, u.page_number, u.start_sec, u.end_sec "
                "FROM chunks c JOIN documents d ON c.document_id=d.id "
                "JOIN extracted_units u ON c.unit_id=u.id WHERE d.status='indexed' AND u.status='reviewed' "
                "ORDER BY d.created_at, d.id, u.page_number, u.start_sec, c.ordinal, c.id"
            )]
        held = {"status": "held", "answer": "登録資料からは確認できません。", "sources": []}
        incompatible = {"status": "held", "answer": "検索用のモデルまたは索引が変わりました。資料を再登録してください。", "sources": []}
        if not rows:
            return held
        dimensions: set[int] = set()
        for row in rows:
            if row["embedding_model"] != self.settings.embedding_model:
                return incompatible
            try:
                vector = json.loads(row["embedding_json"])
            except (TypeError, ValueError):
                return incompatible
            if not isinstance(vector, list) or not vector or len(vector) != row["embedding_dim"] or any(
                not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector
            ):
                return incompatible
            dimensions.add(len(vector))
        if len(dimensions) != 1:
            return incompatible
        with self._model_lock:
            query_vector = self.models.embed(cleaned)
            if not isinstance(query_vector, list) or len(query_vector) not in dimensions or any(
                not isinstance(value, (int, float)) or not math.isfinite(value) for value in query_vector
            ):
                return incompatible
            ranked = rank_chunks(cleaned, query_vector, rows)
            if not ranked:
                return held
            candidates = clarification_candidates(cleaned, ranked)
            if candidates:
                if not self._cited_chunks_are_current(candidates):
                    return held
                return {
                    "status": "needs_clarification",
                    "answer": "該当しそうな資料が複数あります。資料名や識別番号を指定して質問してください。",
                    "sources": [_source_card(row, row["citation_text"][:3000]) for row in candidates],
                }
            evidence = [
                {"id": f"S{index}", "name": item["document_name"], "kind": item["kind"], "text": item["citation_text"][:3000]}
                for index, item in enumerate(ranked, 1)
            ]
            result = self.models.answer(cleaned, evidence)
        answer_value = result.get("answer", "")
        answer = answer_value.strip() if isinstance(answer_value, str) else ""
        selected = result.get("source_ids", [])
        allowed = {item["id"]: ranked[index] for index, item in enumerate(evidence)}
        if not answer or not isinstance(selected, list) or not selected or not all(isinstance(item, str) and item in allowed for item in selected):
            return held
        unique_ids = list(dict.fromkeys(selected))
        cited_text = "\n".join(evidence[int(item[1:]) - 1]["text"] for item in unique_ids)
        if any(number not in cited_text for number in re.findall(r"\d+(?:\.\d+)?", answer)):
            return held
        sources = []
        for source_id in unique_ids:
            row = allowed[source_id]
            sources.append(_source_card(row, evidence[int(source_id[1:]) - 1]["text"]))
        if not self._cited_chunks_are_current([allowed[source_id] for source_id in unique_ids]):
            return held
        return {"status": "answered", "answer": answer, "sources": sources}
