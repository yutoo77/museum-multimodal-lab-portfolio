// Copyright (C) 2026 yutoo77
// SPDX-License-Identifier: AGPL-3.0-only

export type Page = "import" | "documents" | "ask";

export type RetrievalMode = "legacy" | "bm25" | "dense" | "hybrid" | "visual" | "multimodal" | "agentic";

export type UnitKind =
  | "pdf_text"
  | "table"
  | "image_interpretation"
  | "audio_transcript";

export interface ExtractedUnit {
  id: string;
  document_id: string;
  kind: UnitKind;
  extracted_text: string;
  corrected_text: string | null;
  page_number: number | null;
  start_sec: number | null;
  end_sec: number | null;
  model_name: string | null;
  status?: string;
}

export interface ReviewRecord {
  id: string;
  unit_id: string | null;
  action: "edited" | "approved";
  previous_text: string | null;
  new_text: string | null;
  created_at: string;
}

export interface MuseumDocument {
  id: string;
  name: string;
  mime_type: string;
  status: "draft" | "reviewed" | "indexed" | "failed" | "superseded" | string;
  created_at: string;
  updated_at: string;
  version: number;
  extraction_model: string | null;
  units: ExtractedUnit[];
  warning?: string | null;
  error?: string | null;
  reindex_required?: boolean;
  original_available?: boolean;
  page_count?: number | null;
  review_count?: number;
  review_history?: ReviewRecord[];
}

export interface Job {
  id: string;
  kind: "extract" | "approve";
  status: "pending" | "running" | "completed" | "failed";
  progress: number;
  phase?: string;
  elapsed_sec?: number;
  error: string | null;
  document_id?: string;
  created_at?: string;
  started_at?: string | null;
  finished_at?: string | null;
  duration_sec?: number;
}

export interface Source {
  chunk_id: string;
  document_id: string;
  document_name: string;
  unit_id: string;
  kind: UnitKind;
  page_number: number | null;
  start_sec: number | null;
  end_sec: number | null;
  snippet: string;
  original_url: string;
  retrieval_origin?: "visual" | "text";
}

export interface RetrievalTraceEntry {
  action: string;
  mode?: string;
  candidate_count?: number;
  elapsed_ms?: number;
  reason?: string;
}

export interface Answer {
  status: "answered" | "held" | "needs_clarification";
  answer: string;
  sources: Source[];
  retrieval_trace?: RetrievalTraceEntry[];
}

export interface Health {
  status: string;
  ollama: { available: boolean; models: string[] };
  models: {
    answer: string;
    embedding: string;
    vision: string;
    asr: string;
  };
  documents_count: number;
  stale_index_count?: number;
  retrieval_modes?: RetrievalMode[];
  visual_retrieval?: "ready" | "needs_reindex" | "setup_failed" | "disabled";
}
