// Copyright (C) 2026 yutoo77
// SPDX-License-Identifier: AGPL-3.0-only

// Synthetic UI regression: no model call or museum data is used.
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { chromium } from "playwright-core";

const baseUrl = process.env.MML_FRONTEND_URL ?? "http://127.0.0.1:5174/";
if (!/^http:\/\/127\.0\.0\.1:517[45]\/?.*$/.test(baseUrl)) {
  throw new Error("expanded:ui is limited to the local frontend on port 5174 or 5175");
}
const executablePath = [
  process.env.EDGE_PATH,
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
].filter(Boolean).find((path) => existsSync(path));
if (!executablePath) throw new Error("Microsoft Edge not found");

const modes = ["bm25", "legacy", "dense", "hybrid", "visual", "multimodal", "agentic"];
const doc = {
  id: "synthetic-retrieval-document", name: "架空の展示.pdf", mime_type: "application/pdf",
  status: "indexed", created_at: "2026-11-27T00:00:00Z", updated_at: "2026-11-27T00:00:00Z",
  version: 1, extraction_model: "synthetic-model", original_available: true,
  reindex_required: false, review_history: [], units: [],
};
const source = (origin, index) => ({
  chunk_id: `synthetic-${index}`, document_id: doc.id, document_name: doc.name,
  unit_id: `synthetic-unit-${index}`, kind: "pdf_text", page_number: index,
  start_sec: null, end_sec: null, snippet: `確認済みの架空資料 ${index} ページ目`,
  original_url: `/api/documents/${doc.id}/original`, retrieval_origin: origin,
});
const reply = (route, body) => route.fulfill({
  status: 200, contentType: "application/json", body: JSON.stringify(body),
});

const browser = await chromium.launch({ executablePath, headless: true });
const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, locale: "ja-JP" });
const page = await context.newPage();
const errors = [];
const askBodies = [];
let advertisedModes;
page.on("pageerror", (error) => errors.push(error.message));
await page.route("**/api/**", (route) => {
  const request = route.request();
  const path = new URL(request.url()).pathname;
  if (path === "/api/health") return reply(route, {
    status: "ok", ollama: { available: true, models: [] },
    models: { answer: "synthetic", embedding: "synthetic", vision: "synthetic", asr: "synthetic" },
    documents_count: 1, ...(advertisedModes ? { retrieval_modes: advertisedModes } : {}),
  });
  if (path === "/api/documents") return reply(route, { documents: [doc] });
  if (path === "/api/ask") {
    const body = JSON.parse(request.postData());
    askBodies.push(body);
    return reply(route, ["legacy", "bm25"].includes(body.retrieval_mode) ? {
      status: "answered", answer: `${body.retrieval_mode}の回答案`, sources: [source("text", 1)],
    } : {
      status: "answered", answer: "確認済みテキストに基づく回答案",
      sources: [source("visual", 1), source("text", 2)],
      retrieval_trace: [
        { action: "search", mode: body.retrieval_mode, candidate_count: 2, elapsed_ms: 42, reason: "画像と文字から候補を確認" },
        { action: "verify", candidate_count: 1 },
      ],
    });
  }
  throw new Error(`Unexpected synthetic API request: ${request.method()} ${path}`);
});

try {
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: /質問する/ }).click();
  assert.equal(await page.locator(".retrieval-options").evaluate((element) => element.open), false);
  await page.locator(".retrieval-options summary").click();
  const select = page.locator("#retrieval-mode");
  assert.deepEqual(await select.locator("option").evaluateAll((options) => options.map((item) => item.value)), modes.slice(0, 4));
  await page.getByRole("textbox", { name: "質問" }).fill("架空展示について");
  await page.getByRole("button", { name: "資料から調べる" }).click();
  await page.getByText("bm25の回答案").waitFor();
  assert.equal(askBodies.at(-1).retrieval_mode, "bm25");
  assert.equal(await page.locator(".retrieval-trace").count(), 0, "Responses without trace need no trace UI");
  await select.selectOption("legacy");
  await page.getByRole("button", { name: "資料から調べる" }).click();
  await page.getByText("legacyの回答案").waitFor();
  assert.equal(askBodies.at(-1).retrieval_mode, "legacy");

  advertisedModes = modes;
  await page.reload({ waitUntil: "networkidle" });
  await page.locator(".retrieval-options summary").click();
  assert.deepEqual(await select.locator("option").evaluateAll((options) => options.map((item) => item.value)), modes);
  await select.selectOption("visual");
  await page.getByRole("textbox", { name: "質問" }).fill("画像の候補を探す");
  await page.getByRole("button", { name: "資料から調べる" }).click();
  await page.getByText("確認済みテキストに基づく回答案").waitFor();
  assert.equal(askBodies.at(-1).retrieval_mode, "visual");
  assert.deepEqual(await page.locator(".retrieval-origin").allTextContents(), ["画像で候補", "文字で候補"]);
  assert.equal(await page.locator(".source-grounding").textContent(), "回答には確認済みの文字情報を使用");
  const trace = page.locator(".retrieval-trace");
  assert.equal(await trace.evaluate((element) => element.open), false, "Trace starts collapsed");
  await trace.locator("summary").click();
  assert.equal(await trace.evaluate((element) => element.open), true);
  assert.match(await trace.textContent(), /候補 2件/);
  assert.match(await trace.textContent(), /42 ms/);
  assert.match(await trace.textContent(), /画像と文字から候補を確認/);

  await select.selectOption("agentic");
  await page.getByRole("button", { name: "資料から調べる" }).click();
  await page.getByText("確認済みテキストに基づく回答案").waitFor();
  assert.equal(askBodies.at(-1).retrieval_mode, "agentic");
  assert.deepEqual(errors, []);
  console.log("expanded:ui passed: fallback/advertised modes, request mode, optional trace, source origin");
} finally {
  await browser.close();
}
