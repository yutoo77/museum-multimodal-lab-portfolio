// Copyright (C) 2026 yutoo77
// SPDX-License-Identifier: AGPL-3.0-only

// UI state regression with a synthetic, in-browser API. This does not test models or write the DB.
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const baseUrl = process.env.MML_FRONTEND_URL ?? "http://127.0.0.1:5175/";
if (!/^http:\/\/127\.0\.0\.1:517[45]\/?$/.test(baseUrl)) {
  throw new Error("state:ui is limited to the local frontend on port 5174 or 5175");
}
const edgeCandidates = [
  process.env.EDGE_PATH,
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
].filter(Boolean);
const executablePath = edgeCandidates.find((candidate) => existsSync(candidate));
if (!executablePath) throw new Error("Microsoft Edge not found");

const browser = await chromium.launch({ executablePath, headless: true });
const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, locale: "ja-JP" });
const page = await context.newPage();
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));

const now = "2026-11-09T10:00:00Z";
const jobStartedAt = new Date().toISOString();
const unit = {
  id: "synthetic-unit-1", document_id: "synthetic-document-1", kind: "image_interpretation",
  extracted_text: "架空の展示の水温は18℃です。", corrected_text: null,
  page_number: null, start_sec: null, end_sec: null, model_name: "synthetic-model", status: "draft",
};
const doc = {
  id: "synthetic-document-1", name: "架空の水槽.png", mime_type: "image/png", status: "draft",
  created_at: now, updated_at: now, version: 1, extraction_model: "synthetic-model",
  units: [], original_available: true, reindex_required: false, review_history: [],
};
const state = { documents: [], jobs: {}, uploadError: false };
const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL/nwAAAABJRU5ErkJggg==", "base64");
const reply = (route, body, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

await page.route("**/api/**", async (route) => {
  const request = route.request();
  const method = request.method();
  const path = new URL(request.url()).pathname;
  if (path === "/api/health") return reply(route, {
    status: "ok", ollama: { available: true, models: [] },
    models: { answer: "synthetic", embedding: "synthetic", vision: "synthetic", asr: "synthetic" },
    documents_count: state.documents.length, stale_index_count: 0,
  });
  if (path === "/api/documents" && method === "GET") return reply(route, { documents: state.documents });
  if (path === "/api/documents" && method === "POST") {
    if (state.uploadError) return reply(route, { detail: "ファイルを読み取れませんでした。" }, 400);
    state.documents = [doc];
    return reply(route, doc, 201);
  }
  if (path === `/api/documents/${doc.id}` && method === "GET") return reply(route, doc);
  if (path === `/api/documents/${doc.id}/extract` && method === "POST") {
    state.jobs.extract = "running";
    return reply(route, { job_id: "extract" });
  }
  if (path === "/api/jobs/extract" && method === "GET") {
    const status = state.jobs.extract;
    if (status === "completed" && doc.units.length === 0) doc.units = [unit];
    return reply(route, {
      id: "extract", kind: "extract", status, progress: status === "completed" ? 100 : 40,
      phase: status === "completed" ? "読み取りが完了しました" : "画像を読み取っています",
      error: null, document_id: doc.id, created_at: now, started_at: jobStartedAt,
      finished_at: status === "completed" ? new Date().toISOString() : null,
    });
  }
  if (path === `/api/extracted-units/${unit.id}` && method === "PATCH") {
    unit.corrected_text = JSON.parse(request.postData()).corrected_text;
    unit.status = "reviewed";
    doc.status = "reviewed";
    return reply(route, unit);
  }
  if (path === `/api/documents/${doc.id}/approve` && method === "POST") {
    state.jobs.approve = "running";
    return reply(route, { job_id: "approve" });
  }
  if (path === "/api/jobs/approve" && method === "GET") {
    const status = state.jobs.approve;
    if (status === "completed") doc.status = "indexed";
    return reply(route, {
      id: "approve", kind: "approve", status, progress: status === "completed" ? 100 : 50,
      phase: status === "completed" ? "知識への登録が完了しました" : "検索用の情報を作っています",
      error: null, document_id: doc.id, created_at: now, started_at: jobStartedAt,
      finished_at: status === "completed" ? new Date().toISOString() : null,
    });
  }
  if (path === "/api/ask" && method === "POST") {
    const question = JSON.parse(request.postData()).question;
    if (question.includes("資料にない")) return reply(route, { status: "held", answer: "登録資料からは確認できません。", sources: [] });
    return reply(route, { status: "answered", answer: "水温は19℃です。", sources: [{
      chunk_id: "synthetic-chunk-1", document_id: doc.id, document_name: doc.name,
      unit_id: unit.id, kind: unit.kind, page_number: null, start_sec: null, end_sec: null,
      snippet: unit.corrected_text, original_url: `/api/documents/${doc.id}/original`,
    }] });
  }
  if (path === `/api/documents/${doc.id}/original`) return route.fulfill({ status: 200, contentType: "image/png", body: png });
  throw new Error(`Unexpected fake API request: ${method} ${path}`);
});

const nav = (label) => page.getByRole("button", { name: new RegExp(label) });
const expectVisible = async (locator) => {
  await locator.waitFor({ state: "visible", timeout: 10000 });
  assert.equal(await locator.isVisible(), true);
};
const capturePolish = async (name) => {
  if (process.env.MML_CAPTURE_POLISH_UI !== "1") return;
  const target = new URL(`../../docs/ui-review/final-polish/${name}`, import.meta.url);
  await mkdir(new URL(".", target), { recursive: true });
  await page.mouse.move(1, 1);
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await page.waitForTimeout(100);
  await page.screenshot({ path: fileURLToPath(target) });
};
try {
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  for (const label of ["資料を取り込む", "保存した資料", "質問する"]) {
    const navigationButton = page.locator(".nav-item").filter({ hasText: label });
    assert.equal(await navigationButton.locator("svg.app-icon[aria-hidden='true']").count(), 1,
      `The ${label} navigation item must pair a meaningful icon with its visible label`);
  }
  const helpButton = page.getByRole("button", { name: "使い方を開く", exact: true });
  assert.equal(await helpButton.locator("svg.app-icon[aria-hidden='true']").count(), 1,
    "The icon-only help button must have a visible icon and an accessible Japanese name");
  const helpTooltip = page.getByRole("tooltip", { name: "使い方を開く" });
  await helpButton.hover();
  await expectVisible(helpTooltip);
  await page.mouse.move(0, 0);
  await helpButton.focus();
  await expectVisible(helpTooltip);
  await page.keyboard.press("Escape");
  assert.equal(await helpTooltip.isVisible(), false,
    "Escape must dismiss the focused icon tooltip");
  await helpButton.click();
  await expectVisible(page.getByRole("heading", { name: "6つの操作" }));
  await page.keyboard.press("Escape");
  assert.equal(await page.getByRole("heading", { name: "6つの操作" }).count(), 0);

  await nav("質問する").click();
  await expectVisible(page.getByText("まだ質問に使える資料がありません"));
  await nav("資料を取り込む").click();
  await page.locator('.dropzone input[type="file"]').setInputFiles({ name: doc.name, mimeType: "image/png", buffer: png });
  await page.getByRole("button", { name: "資料を追加", exact: true }).click();
  await expectVisible(page.getByRole("heading", { name: "資料を読み取る" }));
  await expectVisible(page.getByText("現行1件", { exact: false }));

  await nav("質問する").click();
  await expectVisible(page.getByText("まだ質問に使える資料がありません"));
  await expectVisible(page.locator(".nav-item.active").filter({ hasText: "質問する" }));
  await capturePolish("12_unapproved_question_FAKE.png");
  if (process.env.MML_CAPTURE_FAKE_UI === "1") {
    const target = new URL("../../docs/ui-review/final/11_question_unapproved_only_FAKE.png", import.meta.url);
    await mkdir(new URL(".", target), { recursive: true });
    await page.mouse.move(1, 1);
    await page.waitForTimeout(250);
    await page.screenshot({ path: fileURLToPath(target) });
  }
  await page.getByRole("button", { name: "内容を確認して登録" }).click();
  await expectVisible(page.getByRole("heading", { name: "資料を読み取る" }));
  await page.getByRole("button", { name: "読み取りを開始" }).click();
  await expectVisible(page.locator(".job-state strong").getByText("画像を読み取っています", { exact: true }));
  await expectVisible(page.locator(".workflow-section .status-pill").getByText("読み取り中", { exact: true }));
  await expectVisible(page.getByText("経過", { exact: false }));
  await nav("保存した資料").click();
  await page.getByRole("button", { name: "処理中 · 状況を見る" }).click();
  await expectVisible(page.getByRole("heading", { name: "資料を取り込む" }));
  const progressBox = await page.locator(".job-state").boundingBox();
  assert.ok(progressBox && progressBox.y >= 0 && progressBox.y + progressBox.height <= 800,
    `Job status must be in the first viewport: ${JSON.stringify(progressBox)}`);
  assert.equal(await page.getByRole("button", { name: "別の資料を追加" }).count(), 0,
    "The add-another-document action must not look available during extraction");
  assert.equal(await page.locator(".upload-panel").count(), 0,
    "The upload form must not remain visible during extraction");
  await expectVisible(page.getByRole("heading", { name: doc.name }));
  await capturePolish("13_extraction_progress_FAKE.png");

  state.jobs.extract = "completed";
  await expectVisible(page.getByRole("heading", { name: "元資料と照らし合わせる" }));
  await nav("質問する").click();
  await expectVisible(page.getByText("まだ質問に使える資料がありません"));
  await page.getByRole("button", { name: "内容を確認して登録" }).click();
  await page.getByRole("textbox", { name: "確認・修正するテキスト" }).fill("架空の展示の水温は19℃です。");
  await page.getByRole("button", { name: "修正を保存" }).click();
  await expectVisible(page.getByText("人の修正を保存済み"));
  await page.getByRole("checkbox", { name: "元資料と抽出内容を確認しました" }).check();
  await page.getByRole("button", { name: "知識に登録", exact: true }).click();
  state.jobs.approve = "completed";
  await expectVisible(page.getByText("知識に登録済み"));
  assert.equal(await page.getByText("検索用の情報を更新しました。質問画面から使えます。").count(), 0,
    "First approval must not be announced as a reindex");
  await nav("保存した資料").click();
  await page.locator(".document-row").filter({ hasText: doc.name }).click();
  await page.getByRole("button", { name: "元資料と内容を比較する" }).click();
  await expectVisible(page.getByRole("heading", { name: "確認済みの内容" }));
  assert.equal(await page.getByRole("heading", { name: "回答に使った箇所" }).count(), 0,
    "Document comparison must not claim that a question used this unit");
  await page.getByRole("button", { name: "← 資料一覧へ戻る" }).click();
  await nav("資料を取り込む").click();
  await page.getByRole("button", { name: "資料に質問する", exact: true }).click();
  await page.getByRole("textbox", { name: "質問" }).fill("水温は何℃ですか？");
  await page.getByRole("button", { name: "資料から調べる" }).click();
  await expectVisible(page.getByRole("heading", { name: "回答案" }));
  await expectVisible(page.getByText("水温は19℃です。", { exact: true }));
  await page.getByRole("button", { name: "元資料と比較する" }).click();
  await expectVisible(page.getByRole("heading", { name: "根拠を元資料で確認" }));
  await expectVisible(page.getByText("架空の展示の水温は19℃です。"));
  await expectVisible(page.getByRole("img", { name: `${doc.name} の元画像` }));
  await page.getByRole("button", { name: "← 質問結果へ戻る" }).click();
  await expectVisible(page.getByRole("heading", { name: "回答案" }));
  await expectVisible(page.getByText("水温は19℃です。", { exact: true }));

  await page.getByRole("textbox", { name: "質問" }).fill("資料にない質問");
  await page.getByRole("button", { name: "資料から調べる" }).click();
  await expectVisible(page.getByRole("heading", { name: "登録資料からは確認できません" }));

  await nav("資料を取り込む").click();
  await page.getByRole("button", { name: "別の資料を追加" }).click();
  state.uploadError = true;
  await page.locator('.dropzone input[type="file"]').setInputFiles({ name: "破損ファイル.pdf", mimeType: "application/pdf", buffer: Buffer.from("not pdf") });
  await page.getByRole("button", { name: "資料を追加", exact: true }).click();
  await expectVisible(page.getByRole("button", { name: "ファイルを選び直す" }));
  await page.getByRole("button", { name: "ファイルを選び直す" }).click();
  await expectVisible(page.locator('.dropzone input[type="file"]'));
  assert.equal(await page.locator('.dropzone input[type="file"]').inputValue(), "");

  assert.deepEqual(errors, [], `Browser errors: ${errors.join(" | ")}`);
  console.log(JSON.stringify({ kind: "FAKE_API_UI_STATE_TEST", result: "PASS", checks: ["navigation_icons_with_labels", "help_icon_hover_focus_escape_tooltip", "help", "empty", "upload_count", "unapproved", "job_link_viewport", "job_action_hidden", "review", "approval", "document_comparison_heading", "answer", "source_comparison_return", "held", "upload_error_recovery"] }, null, 2));
} finally {
  await browser.close();
}
