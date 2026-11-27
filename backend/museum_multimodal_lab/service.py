# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from .advanced_retrieval import DEFAULT_RETRIEVAL_MODE, RETRIEVAL_MODES, rank_text_modes
from .agentic import run_agentic
from .config import Settings
from .db import Database
from .extraction import ExtractionError, extract_document, validate_upload
from .providers import AsrProvider, ModelError, ModelProvider
from .retrieval import (
    chunk_text,
    clarification_candidates,
    lexical_score,
    normalize,
    rank_chunks,
    terms,
)
from .visual_retrieval import (
    VisualEncoder,
    fuse_pages,
    read_visual_page,
    visual_pages,
    visual_rank,
)


class NotFoundError(ValueError):
    pass


class ConflictError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _uuid() -> str:
    return str(uuid.uuid4())


def _numeric_tokens(text: str) -> set[str]:
    # Require complete numbers; '42' must not be validated by '142' or '-42'.
    return set(re.findall(r"[+-]?\d+(?:[.,]\d+)*", normalize(text)))


_COLOR_REQUEST = re.compile(r"何色|なにいろ|どんな色|どの色|色(?:は(?:何|なに|どれ|[？?]|$)|を(?:教え|示し|確認|答え))")
_COLOR_TOKEN = re.compile(
    r"青緑|黄緑|水色|紺色|群青|藍色|灰色|銀色|金色|桃色|橙色|無色|透明|"
    r"ブルー|レッド|グリーン|イエロー|ホワイト|ブラック|ピンク|オレンジ|"
    r"[赤青緑黄白黒紫茶](?=色|い|線|の|で|だ|と|に|が|は|、|。|\s|$)"
)
_COLOR_ALIAS = {
    "ブルー": "青", "レッド": "赤", "グリーン": "緑", "イエロー": "黄",
    "ホワイト": "白", "ブラック": "黒", "ピンク": "桃色", "オレンジ": "橙色",
}


def _color_tokens(text: str) -> set[str]:
    return {_COLOR_ALIAS.get(token, token) for token in _COLOR_TOKEN.findall(normalize(text))}


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
        **({"retrieval_origin": row["retrieval_origin"]} if "retrieval_origin" in row else {}),
        "original_url": f"/api/documents/{row['document_id']}/original{fragment}",
    }


class LabService:
    def __init__(self, settings: Settings, models: ModelProvider, asr: AsrProvider, visual: VisualEncoder | None = None):
        self.settings = settings
        self.models = models
        self.asr = asr
        self.originals = settings.data_dir / "originals"
        self.originals.mkdir(parents=True, exist_ok=True)
        self.db = Database(settings.data_dir / "museum_multimodal_lab.sqlite3")
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mml-job")
        self._submit_lock = threading.Lock()
        self._model_lock = threading.Lock()
        self._qdrant = None
        self.visual = visual
        self.visual_setup_failed = False
        if self.visual is None and settings.visual_model_dir is not None:
            try:
                from .visual_model import LocalVisualModel

                self.visual = LocalVisualModel(settings.visual_model_dir)
            except Exception:  # noqa: BLE001 - optional third-party runtime boundary
                # A missing model/incompatible runtime does not disable text retrieval.
                self.visual_setup_failed = True

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)
        if self._qdrant is not None:
            self._qdrant.close()

    def _local_vector_index(self):
        if self._qdrant is None:
            try:
                from .qdrant_index import QdrantIndex

                self._qdrant = QdrantIndex(self.settings.data_dir)
            except Exception as exc:
                raise ModelError("ローカルQdrantを開けません。索引の保存先と依存関係を確認してください") from exc
        return self._qdrant

    def health(self) -> dict[str, Any]:
        try:
            installed = self.models.models()
            available = True
        except ModelError:
            installed, available = [], False
        with self.db.connect() as connection:
            count = connection.execute("SELECT count(*) FROM documents WHERE status != 'superseded'").fetchone()[0]
            visual_pending = self.visual is not None and connection.execute(
                "SELECT 1 FROM chunks c JOIN documents d ON c.document_id=d.id "
                "JOIN extracted_units u ON c.unit_id=u.id LEFT JOIN visual_pages v ON v.chunk_id=c.id "
                "WHERE d.status='indexed' AND u.status='reviewed' AND "
                "((d.mime_type='application/pdf' AND u.page_number>0) OR d.mime_type LIKE 'image/%') "
                "GROUP BY d.id, COALESCE(u.page_number, 0) "
                "HAVING SUM(CASE WHEN v.model=? AND v.embedding_dim=? THEN 1 ELSE 0 END) != 1 LIMIT 1",
                (self.visual.model_id, self.visual.dimension),
            ).fetchone() is not None
        modes = list(RETRIEVAL_MODES)
        if self.visual is not None and not visual_pending:
            modes += ["visual", "multimodal"]
        if available and callable(getattr(self.models, "plan_retrieval", None)) and (
            self.settings.answer_model in installed or self.settings.answer_model.removesuffix(":latest") in installed
        ):
            modes.append("agentic")
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
            "retrieval_modes": modes,
            "visual_retrieval": self._visual_status(visual_pending),
        }

    def _visual_status(self, pending: bool) -> str:
        if self.visual is not None:
            return "needs_reindex" if pending else "ready"
        return "setup_failed" if self.visual_setup_failed else "disabled"

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
            mime_type, stored_name = row["mime_type"], row["stored_name"]
        staged: list[tuple[str, str, int, str, list[float]]] = []
        staged_visual: list[tuple[str, str, int, str]] = []
        visual_failed = False
        with self._model_lock:
            for unit in units:
                content = unit["corrected_text"] if unit["corrected_text"] is not None else unit["extracted_text"]
                for ordinal, piece in enumerate(chunk_text(content, unit["kind"])):
                    if len(piece) > 8000:
                        raise ExtractionError("抽出結果が長すぎます。分割して登録してください")
                    vector = self.models.embed(piece)
                    staged.append((_uuid(), unit["id"], ordinal, piece, vector))
            if self.visual is not None and (mime_type == "application/pdf" or mime_type.startswith("image/")):
                page_by_unit = {unit["id"]: unit["page_number"] or 0 for unit in units}
                seen: set[int] = set()
                try:
                    for chunk_id, unit_id, *_ in staged:
                        page = page_by_unit[unit_id]
                        if page in seen or (mime_type == "application/pdf" and page == 0):
                            continue
                        with read_visual_page(self._stored_path(stored_name), mime_type, page) as image:
                            vector = self.visual.encode_image(image)
                        if len(vector) != self.visual.dimension or not any(vector) or not all(math.isfinite(x) for x in vector):
                            raise ValueError("invalid visual embedding")
                        staged_visual.append((chunk_id, self.visual.model_id, self.visual.dimension, json.dumps(vector)))
                        seen.add(page)
                except Exception:  # noqa: BLE001 - optional visual runtime must not invalidate approved text
                    # Commit the existing text workflow, never a partial visual index.
                    visual_failed = True
                    staged_visual.clear()
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
            connection.executemany(
                "INSERT INTO visual_pages(chunk_id,model,embedding_dim,embedding_json) VALUES(?,?,?,?)", staged_visual,
            )
            connection.execute("UPDATE extracted_units SET status='reviewed', updated_at=? WHERE document_id=?", (now, document_id))
            warning = "視覚索引を作成できませんでした。設定を確認してこの資料を再索引してください。" if visual_failed else None
            connection.execute("UPDATE documents SET status='indexed', error=?, updated_at=? WHERE id=?", (warning, now, document_id))
        if staged_visual and self.visual is not None:
            try:
                with self._model_lock:
                    proxies = [self._visual_proxy(item) for item in self._current_chunks()
                               if item["visual_model"] == self.visual.model_id and item["visual_dim"] == self.visual.dimension]
                    self._local_vector_index().reconcile(proxies, self.visual.model_id, self.visual.dimension, space="visual")
            except Exception:  # noqa: BLE001 - derived cache only; text approval already committed
                # Text and cached visual vectors are already committed. Search
                # retries the derived index, and must not return partial hits.
                with self.db.connect() as connection:
                    connection.execute(
                        "UPDATE documents SET error=? WHERE id=? AND updated_at=?",
                        ("視覚索引の保存に失敗しました。視覚検索時に再試行します。", document_id, now),
                    )
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

    def _current_chunks(self) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT c.*, d.name AS document_name, d.version, d.mime_type, u.kind, u.page_number, u.start_sec, u.end_sec, "
                "v.model AS visual_model, v.embedding_dim AS visual_dim, v.embedding_json AS visual_embedding "
                "FROM chunks c JOIN documents d ON c.document_id=d.id "
                "JOIN extracted_units u ON c.unit_id=u.id LEFT JOIN visual_pages v ON v.chunk_id=c.id "
                "WHERE d.status='indexed' AND u.status='reviewed' "
                "ORDER BY d.created_at, d.id, u.page_number, u.start_sec, c.ordinal, c.id"
            )]

    @staticmethod
    def _visual_proxy(row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row, "embedding_json": row["visual_embedding"], "embedding_model": row["visual_model"],
            "embedding_dim": row["visual_dim"],
        }

    def _matches_visual_model(self, row: dict[str, Any]) -> bool:
        return self.visual is not None and row["visual_model"] == self.visual.model_id and row["visual_dim"] == self.visual.dimension

    def _rank_visual(self, question: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.visual is None:
            raise ModelError("視覚検索モデルが未設定、または読み込めません")
        pages = visual_pages(rows)
        if not pages:
            if (self.settings.data_dir / "qdrant").exists():
                try:
                    self._local_vector_index().clear_when_empty(space="visual")
                except Exception as exc:
                    raise ModelError("ローカルQdrantの視覚索引を整理できません") from exc
            return []
        proxies = []
        for group in pages.values():
            saved = [row for row in group if self._matches_visual_model(row)]
            if len(saved) != 1:
                raise ModelError("現行モデルの視覚索引がない画像・PDFがあります。対象資料を再索引してください")
            row = saved[0]
            group.remove(row)
            group.insert(0, row)
            proxies.append(self._visual_proxy(row))
        try:
            vector = self.visual.encode_text(question)
            if len(vector) != self.visual.dimension:
                raise ValueError("incompatible visual query dimension")
            hits = self._local_vector_index().query(vector, proxies, self.visual.model_id, limit=len(proxies), space="visual")
        except Exception as exc:
            raise ModelError("ローカル視覚モデルまたはQdrantの検索に失敗しました") from exc
        return visual_rank(question, pages, hits)

    def _rank(
        self, question: str, mode: str, rows: list[dict[str, Any]], dimension: int, timeout: float | None = None,
    ) -> list[dict[str, Any]] | None:
        if mode in {"visual", "multimodal"}:
            started = time.monotonic()
            visual = self._rank_visual(question, rows)
            if mode == "visual":
                return visual
            remaining = None if timeout is None else timeout - (time.monotonic() - started)
            if remaining is not None and remaining <= 0:
                raise TimeoutError("visual search reached the remaining budget")
            text = self._rank(question, "hybrid", rows, dimension, remaining)
            return None if text is None else fuse_pages(text, visual)
        if mode == "bm25":
            return rank_text_modes(question, rows, "bm25")
        bounded = getattr(self.models, "embed_bounded", None)
        vector = bounded(question, timeout) if timeout is not None and callable(bounded) else self.models.embed(question)
        if not isinstance(vector, list) or len(vector) != dimension or any(
            not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector
        ):
            return None
        if mode == "legacy":
            return rank_chunks(question, vector, rows)
        try:
            dense_hits = self._local_vector_index().query(vector, rows, self.settings.embedding_model, limit=len(rows))
        except Exception as exc:
            raise ModelError("ローカルQdrantの照合・検索に失敗しました。従来方式またはBM25を選べます") from exc
        return rank_text_modes(question, rows, mode, dense_hits)

    def _visual_assist(self, question: str, ranked: list[dict[str, Any]], rows: list[dict[str, Any]]) -> str:
        """A local VLM may select an approved excerpt, never supply new facts."""
        for index, row in enumerate(ranked):
            if row.get("retrieval_origin") != "visual":
                continue
            choices = next((group for group in visual_pages(rows).values() if any(item["id"] == row["id"] for item in group)), [])
            choices = [
                choice for choice in choices if choice["unit_id"] == row["unit_id"]
                and (choice["id"] == row["id"] or all(item["id"] != choice["id"] for item in ranked))
            ]
            if len(choices) < 2:
                continue
            # Do not replace evidence for a question containing an exact record ID.
            if any(re.search(r"[a-z].*\d", token) for token in terms(question)):
                continue
            try:
                path, mime, _ = self.original_path(row["document_id"])
                with read_visual_page(path, mime, row["page_number"] or 0) as image, BytesIO() as output:
                    image.save(output, format="PNG")
                    draft = self.models.inspect_image(output.getvalue())
            except (ModelError, ValueError, OSError):
                return "assist_unavailable"
            observation = terms(draft)
            query = terms(question)
            picked = max(choices, key=lambda item: (
                lexical_score(query, terms(item["search_text"])),
                lexical_score(observation, terms(item["search_text"])),
            ))
            ranked[index] = {**row, **picked}
            return "approved_excerpt_selected"
        return "no_eligible_excerpt"

    def ask(self, question: str, retrieval_mode: str = DEFAULT_RETRIEVAL_MODE) -> dict[str, Any]:
        cleaned = question.strip()
        if not cleaned or len(cleaned) > 1500:
            raise ExtractionError("質問は1〜1500文字で入力してください")
        if retrieval_mode not in (*RETRIEVAL_MODES, "visual", "multimodal", "agentic"):
            raise ExtractionError("検索方法が不正です")
        if retrieval_mode in {"visual", "multimodal"} and self.visual is None:
            raise ModelError("視覚検索モデルが未設定、または読み込めません")
        deadline = time.monotonic() + 80.0 if retrieval_mode == "agentic" else None
        trace: list[dict[str, Any]] = []

        def report(result: dict[str, Any]) -> dict[str, Any]:
            return {**result, "retrieval_trace": trace} if trace else result

        rows = self._current_chunks()
        held = {"status": "held", "answer": "登録資料からは確認できません。", "sources": []}
        incompatible = {"status": "held", "answer": "検索用のモデルまたは索引が変わりました。資料を再登録してください。", "sources": []}
        if not rows:
            if retrieval_mode in {"dense", "hybrid", "visual", "multimodal"} and (self.settings.data_dir / "qdrant").exists():
                try:
                    with self._model_lock:
                        if retrieval_mode != "visual":
                            self._local_vector_index().clear_when_empty()
                        if retrieval_mode in {"visual", "multimodal"}:
                            self._local_vector_index().clear_when_empty(space="visual")
                except Exception as exc:
                    raise ModelError("ローカルQdrantの空索引を整理できません。Legacy検索は引き続き利用できます") from exc
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
        dimension = next(iter(dimensions))
        acquired = self._model_lock.acquire(timeout=max(0.0, deadline - time.monotonic()) if deadline else -1)
        if not acquired:
            return report({**held, "answer": "ローカルモデルの待ち時間が上限に達しました。"})
        try:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return report(held)
                visual_ready = self.visual is not None and all(
                    sum(self._matches_visual_model(row) for row in group) == 1 for group in visual_pages(rows).values()
                )
                modes = ("bm25", "hybrid", "visual", "multimodal") if visual_ready else ("bm25", "hybrid")
                plan = getattr(self.models, "plan_retrieval", None)

                def search(q: str, mode: str, timeout: float) -> list[dict[str, Any]]:
                    hits = self._rank(q, mode, rows, dimension, timeout) or []
                    return hits if self._cited_chunks_are_current(hits) else []

                agent = run_agentic(
                    cleaned, search,
                    plan if callable(plan) else None, modes=modes, budget_seconds=min(55.0, remaining * 0.7),
                    stop_early=lambda hits: bool(clarification_candidates(cleaned, hits)),
                )
                ranked = agent.ranked
                trace.extend({
                    "action": step["stage"], "mode": step.get("mode"), "candidate_count": step.get("hit_count"),
                    "elapsed_ms": step["elapsed_ms"], "reason": step["status"],
                    "rewritten": step.get("rewritten", False), "fallback": step.get("fallback", False),
                } for step in agent.trace)
                trace.append({"action": "stop", "reason": agent.stop_reason})
            else:
                ranked = self._rank(cleaned, retrieval_mode, rows, dimension)
                if ranked is None:
                    return incompatible
            if not ranked:
                return report(held)
            candidates = clarification_candidates(cleaned, ranked)
            if candidates:
                if not self._cited_chunks_are_current(candidates):
                    return report(held)
                return report({
                    "status": "needs_clarification",
                    "answer": "該当しそうな資料が複数あります。資料名や識別番号を指定して質問してください。",
                    "sources": [_source_card(row, row["citation_text"][:3000]) for row in candidates],
                })
            if not self._cited_chunks_are_current(ranked):
                return report(held)
            if self.settings.visual_assist and retrieval_mode in {"visual", "multimodal"}:
                trace.append({"action": "visual_assist", "reason": self._visual_assist(cleaned, ranked, rows)})
                if not self._cited_chunks_are_current(ranked):
                    return report(held)
            evidence = [
                {"id": f"S{index}", "name": item["document_name"], "kind": item["kind"], "text": item["citation_text"][:3000]}
                for index, item in enumerate(ranked, 1)
            ]
            if deadline is None:
                result = self.models.answer(cleaned, evidence)
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return report(held)
                started = time.monotonic()
                bounded = getattr(self.models, "answer_bounded", None)
                try:
                    result = bounded(cleaned, evidence, remaining) if callable(bounded) else self.models.answer(cleaned, evidence)
                except (ModelError, TimeoutError):
                    trace.append({"action": "answer", "reason": "model_unavailable", "elapsed_ms": round((time.monotonic() - started) * 1000)})
                    return report(held)
                trace.append({"action": "answer", "elapsed_ms": round((time.monotonic() - started) * 1000)})
                if time.monotonic() >= deadline:
                    return report(held)
        finally:
            self._model_lock.release()
        answer_value = result.get("answer", "")
        answer = answer_value.strip() if isinstance(answer_value, str) else ""
        selected = result.get("source_ids", [])
        allowed = {item["id"]: ranked[index] for index, item in enumerate(evidence)}
        if not answer or not isinstance(selected, list) or not selected or not all(isinstance(item, str) and item in allowed for item in selected):
            return report(held)
        unique_ids = list(dict.fromkeys(selected))
        cited_text = "\n".join(evidence[int(item[1:]) - 1]["text"] for item in unique_ids)
        if not _numeric_tokens(answer).issubset(_numeric_tokens(cited_text)):
            return report(held)
        if _COLOR_REQUEST.search(cleaned):
            # A page match is not an observation. Check only the selected
            # approved excerpts, not the pixels or uncited candidate text.
            colors = _color_tokens(answer)
            if not colors or not colors.issubset(_color_tokens(cited_text)):
                return report(held)
        sources = []
        for source_id in unique_ids:
            row = allowed[source_id]
            sources.append(_source_card(row, evidence[int(source_id[1:]) - 1]["text"]))
        # Every excerpt can influence generation, even if the model cites
        # another source. Invalidate the draft if any input became stale.
        if not self._cited_chunks_are_current(ranked):
            return report(held)
        return report({"status": "answered", "answer": answer, "sources": sources})
