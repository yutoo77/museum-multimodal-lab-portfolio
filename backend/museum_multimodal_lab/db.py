# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  stored_name TEXT NOT NULL UNIQUE,
  sha256 TEXT NOT NULL,
  version INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('draft','reviewed','indexed','failed','superseded')),
  extraction_model TEXT,
  warning TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_documents_name ON documents(name);
CREATE TABLE IF NOT EXISTS extracted_units (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK(kind IN ('pdf_text','table','image_interpretation','audio_transcript')),
  extracted_text TEXT NOT NULL,
  corrected_text TEXT,
  page_number INTEGER,
  bbox_json TEXT,
  start_sec REAL,
  end_sec REAL,
  model_name TEXT,
  status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','reviewed')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_units_document ON extracted_units(document_id);
CREATE TABLE IF NOT EXISTS review_events (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  unit_id TEXT,
  action TEXT NOT NULL CHECK(action IN ('edited','approved')),
  previous_text TEXT,
  new_text TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_review_events_document ON review_events(document_id, created_at);
CREATE TABLE IF NOT EXISTS chunks (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  unit_id TEXT NOT NULL REFERENCES extracted_units(id) ON DELETE CASCADE,
  search_text TEXT NOT NULL,
  citation_text TEXT NOT NULL,
  embedding_json TEXT NOT NULL,
  embedding_model TEXT NOT NULL,
  embedding_dim INTEGER NOT NULL DEFAULT 0,
  index_version TEXT NOT NULL,
  ordinal INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_chunks_document ON chunks(document_id);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK(kind IN ('extract','approve')),
  status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed')),
  progress REAL NOT NULL,
  error TEXT,
  started_at TEXT,
  finished_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_jobs_document ON jobs(document_id);
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            # Small, additive migrations preserve previously indexed local data.
            columns = {row[1] for row in connection.execute("PRAGMA table_info(chunks)")}
            if "embedding_dim" not in columns:
                connection.execute("ALTER TABLE chunks ADD COLUMN embedding_dim INTEGER NOT NULL DEFAULT 0")
            if "ordinal" not in columns:
                connection.execute("ALTER TABLE chunks ADD COLUMN ordinal INTEGER NOT NULL DEFAULT 0")
            for chunk_id, raw in connection.execute("SELECT id, embedding_json FROM chunks WHERE embedding_dim=0"):
                try:
                    vector = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if isinstance(vector, list) and vector:
                    connection.execute("UPDATE chunks SET embedding_dim=? WHERE id=?", (len(vector), chunk_id))
            # A process restart cannot safely resume a model call. Preserve the failure.
            interrupted_documents = [row[0] for row in connection.execute(
                "SELECT DISTINCT document_id FROM jobs WHERE status IN ('pending','running')"
            )]
            interrupted_at = datetime.now(UTC).isoformat(timespec="microseconds")
            connection.execute(
                "UPDATE jobs SET status='failed', error='アプリの再起動で処理が中断しました', "
                "finished_at=?, updated_at=? WHERE status IN ('pending','running')",
                (interrupted_at, interrupted_at),
            )
            for document_id in interrupted_documents:
                connection.execute(
                    "UPDATE documents SET error='アプリの再起動で処理が中断しました', updated_at=? WHERE id=?",
                    (interrupted_at, document_id),
                )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
