// Copyright (C) 2026 yutoo77
// SPDX-License-Identifier: AGPL-3.0-only

// Read-only browser capture of the normal local demo. Refuse non-fictional data.
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { mkdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const output = path.join(root, "docs", "ui-review", "final-polish");
const baseUrl = process.env.MML_FRONTEND_URL || "http://127.0.0.1:5174/";
if (!/^http:\/\/127\.0\.0\.1:5174\/?$/.test(baseUrl)) throw new Error("Only the local 5174 demo is allowed");
const edge = [
  process.env.EDGE_PATH,
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
].find((candidate) => candidate && existsSync(candidate));
if (!edge) throw new Error("Microsoft Edge was not found");

const browser = await chromium.launch({ executablePath: edge, headless: true });
const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, locale: "ja-JP" });
const page = await context.newPage();
const pageErrors = [];
const screenshots = [];
page.on("pageerror", (error) => pageErrors.push(error.message));

const capture = async (name) => {
  await page.mouse.move(2, 2);
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await page.waitForTimeout(120);
  const file = path.join(output, name);
  await page.screenshot({ path: file });
  screenshots.push(name);
};
const scrollTop = () => page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
const nav = (label) => page.getByRole("navigation", { name: "画面" }).getByRole("button", { name: label, exact: true }).click();
const noOverflow = async (label) => assert.equal(
  await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1),
  false, `${label}: horizontal overflow`,
);

try {
  const documentsResponse = await page.request.get(new URL("/api/documents", baseUrl).toString());
  assert.equal(documentsResponse.status(), 200);
  const documents = (await documentsResponse.json()).documents;
  assert.ok(documents.length > 0, "The synthetic demo documents are missing");
  assert.ok(documents.every((doc) => /^fictional_[\w.-]+$/.test(doc.name)), "Non-fictional document found; capture aborted");
  const image = documents.find((doc) => doc.name === "fictional_lumina_table.png" && doc.status === "indexed");
  assert.ok(image, "The indexed fictional image is required");

  await mkdir(output, { recursive: true });
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.getByRole("heading", { name: "資料を取り込む" }).waitFor();
  await scrollTop();
  await capture("01_import_empty_live.png");
  await page.getByRole("button", { name: "使い方を開く", exact: true }).click();
  await page.getByRole("heading", { name: "6つの操作" }).waitFor();
  await capture("02_help_live.png");
  await page.locator(".help-panel").getByRole("button", { name: "使い方を閉じる", exact: true }).click();

  await nav("保存した資料");
  await page.getByRole("heading", { name: "保存した資料", exact: true }).waitFor();
  await scrollTop();
  await capture("03_documents_live.png");
  await page.locator(".document-row").filter({ hasText: image.name }).click();
  await page.locator(".document-detail .image-preview").evaluate((img) => img.decode());
  await scrollTop();
  await capture("04_image_detail_live.png");

  await page.getByRole("button", { name: "確認・編集する" }).click();
  await page.locator(".review-grid").waitFor();
  await page.locator(".review-grid .image-preview").evaluate((img) => img.decode());
  await page.locator(".step-heading").evaluate((element) => element.scrollIntoView({ block: "start", behavior: "instant" }));
  await capture("05_original_review_live.png");

  await nav("質問する");
  await page.getByRole("textbox", { name: "質問" }).waitFor();
  await scrollTop();
  await capture("06_question_live.png");
  await page.getByRole("textbox", { name: "質問" }).fill("ルミナ水槽の水温は何℃ですか？");
  const responsePromise = page.waitForResponse(
    (response) => response.request().method() === "POST" && new URL(response.url()).pathname === "/api/ask",
    { timeout: 120000 },
  );
  const started = Date.now();
  await page.getByRole("button", { name: "資料から調べる" }).click();
  const response = await responsePromise;
  const answerSeconds = Number(((Date.now() - started) / 1000).toFixed(2));
  assert.equal(response.status(), 200, `Question API returned ${response.status()}`);
  const answer = await response.json();
  assert.equal(answer.status, "answered");
  assert.match(answer.answer, /17\s*℃/);
  assert.ok(answer.sources.some((source) => source.document_id === image.id));
  await page.locator(".source-card").filter({ hasText: image.name }).waitFor();
  await page.locator(".answer-section").evaluate((element) => element.scrollIntoView({ block: "start", behavior: "instant" }));
  await capture("07_answer_sources_live.png");
  await noOverflow("answer 1280");

  await page.getByRole("button", { name: "元資料と比較する" }).first().click();
  await page.getByRole("heading", { name: "根拠を元資料で確認" }).waitFor();
  await page.locator(".source-comparison-grid .image-preview").evaluate((img) => img.decode());
  await scrollTop();
  await capture("08_source_comparison_live.png");
  await noOverflow("comparison 1280");

  await page.setViewportSize({ width: 1024, height: 768 });
  await scrollTop();
  await capture("09_source_comparison_1024_live.png");
  await noOverflow("comparison 1024");
  await page.getByRole("button", { name: "← 質問結果へ戻る" }).click();
  await page.locator(".answer-card").waitFor();
  await page.locator(".answer-section").evaluate((element) => element.scrollIntoView({ block: "start", behavior: "instant" }));
  await capture("10_answer_1024_live.png");
  await noOverflow("answer 1024");
  await nav("資料を取り込む");
  await scrollTop();
  await capture("11_import_1024_live.png");
  await noOverflow("import 1024");

  assert.deepEqual(pageErrors, []);
  console.log(JSON.stringify({ kind: "LIVE_LOCAL_DEMO_READ_ONLY", screenshots, answerSeconds, pageErrors }, null, 2));
} finally {
  await browser.close();
}
