// Copyright (C) 2026 yutoo77
// SPDX-License-Identifier: AGPL-3.0-only

import type { Answer, ExtractedUnit, Health, Job, MuseumDocument } from "./types";

const request = async <T>(path: string, init?: RequestInit): Promise<T> => {
  let response: Response;
  try {
    response = await fetch(path, { cache: "no-store", credentials: "omit", ...init });
  } catch {
    throw new Error("アプリの処理サーバーへ接続できません。起動状態を確認してください。");
  }
  if (!response.ok) {
    let detail = `処理に失敗しました（HTTP ${response.status}）。`;
    try {
      const body = (await response.json()) as { detail?: unknown; message?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
      else if (typeof body.message === "string") detail = body.message;
      else if (Array.isArray(body.detail)) {
        detail = body.detail.map((item: { msg?: string }) => item.msg ?? "入力を確認してください").join(" / ");
      }
    } catch {
      // Response did not contain JSON; retain the HTTP status message.
    }
    throw new Error(detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
};

const json = (body: unknown): RequestInit => ({
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const api = {
  health: () => request<Health>("/api/health"),
  listDocuments: async () => (await request<{ documents: MuseumDocument[] }>("/api/documents")).documents,
  getDocument: (id: string) => request<MuseumDocument>(`/api/documents/${encodeURIComponent(id)}`),
  uploadDocument: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<MuseumDocument>("/api/documents", { method: "POST", body: form });
  },
  extractDocument: (id: string) =>
    request<{ job_id: string }>(`/api/documents/${encodeURIComponent(id)}/extract`, { method: "POST" }),
  getJob: (id: string) => request<Job>(`/api/jobs/${encodeURIComponent(id)}`),
  updateUnit: (id: string, correctedText: string) =>
    request<ExtractedUnit>(`/api/extracted-units/${encodeURIComponent(id)}`, {
      method: "PATCH",
      ...json({ corrected_text: correctedText }),
    }),
  approveDocument: (id: string) =>
    request<{ job_id: string } | MuseumDocument>(`/api/documents/${encodeURIComponent(id)}/approve`, { method: "POST" }),
  reindexDocument: (id: string) =>
    request<{ job_id: string }>(`/api/documents/${encodeURIComponent(id)}/reindex`, { method: "POST" }),
  deleteDocument: (id: string) =>
    request<void>(`/api/documents/${encodeURIComponent(id)}`, { method: "DELETE" }),
  ask: (question: string) => request<Answer>("/api/ask", { method: "POST", ...json({ question }) }),
  originalUrl: (id: string) => `/api/documents/${encodeURIComponent(id)}/original`,
};
