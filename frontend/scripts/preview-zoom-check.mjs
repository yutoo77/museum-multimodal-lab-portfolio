// Copyright (C) 2026 yutoo77
// SPDX-License-Identifier: AGPL-3.0-only

// Isolated browser UI check. All API data and preview images are fictional fakes.
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { mkdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const baseUrl = process.env.MML_FRONTEND_URL ?? "http://127.0.0.1:5174/";
if (!/^http:\/\/127\.0\.0\.1:517[45]\/?$/.test(baseUrl)) throw new Error("Local test frontend only");
const edge = [process.env.EDGE_PATH, "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe", "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe"].filter(Boolean).find(existsSync);
if (!edge) throw new Error("Microsoft Edge was not found");

const now = "2026-11-14T08:00:00Z";
const makeDocument = (id, name, mime, kind, pageNumber) => ({
  id, name, mime_type: mime, status: "indexed", created_at: now, updated_at: now,
  version: 1, extraction_model: "FAKE UI fixture", original_available: true,
  reindex_required: false, review_history: [], page_count: mime === "application/pdf" ? 2 : null,
  units: [{ id: `${id}-unit`, document_id: id, kind, extracted_text: "架空展示の水温は17℃です。",
    corrected_text: "架空展示の水温は17℃です。", status: "reviewed", page_number: pageNumber,
    start_sec: null, end_sec: null, model_name: "FAKE UI fixture" }],
});
const imageDoc = makeDocument("fake-image", "架空の水槽図.png", "image/png", "image_interpretation", null);
const pdfDoc = makeDocument("fake-pdf", "架空の展示案内.pdf", "application/pdf", "pdf_text", 2);
const docs = [imageDoc, pdfDoc];
const svg = (label) => Buffer.from(`<svg xmlns="http://www.w3.org/2000/svg" width="900" height="650" viewBox="0 0 900 650"><rect width="900" height="650" fill="#fff"/><rect x="38" y="34" width="824" height="580" fill="#f2f9fc" stroke="#84b6ce" stroke-width="3"/><text x="65" y="100" font-family="sans-serif" font-size="34" fill="#23556d">架空資料 / FAKE UI TEST</text><text x="65" y="195" font-family="sans-serif" font-size="38" fill="#1e485f">${label}</text><path d="M65 230h765" stroke="#84b6ce" stroke-width="2"/><text x="65" y="345" font-family="sans-serif" font-size="54" fill="#125f88">水温 17℃</text><text x="65" y="430" font-family="sans-serif" font-size="30" fill="#355966">出典確認の操作テスト用</text></svg>`, "utf8");
const reply = (route, value) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(value) });

const browser = await chromium.launch({ executablePath: edge, headless: true });
const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, locale: "ja-JP" });
const output = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../outputs/portfolio-final-ui/after");
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
const capture = async (name) => {
  if (process.env.MML_CAPTURE_PREVIEW_UI !== "1") return;
  await mkdir(output, { recursive: true });
  await page.mouse.move(0, 0);
  await page.screenshot({ path: path.join(output, name), fullPage: false });
};

await page.route("**/api/**", async (route) => {
  const request = route.request();
  const url = new URL(request.url());
  const pathName = url.pathname;
  if (pathName === "/api/health") return reply(route, { status: "ok", ollama: { available: true, models: [] }, models: { answer: "FAKE", embedding: "FAKE", vision: "FAKE", asr: "FAKE" }, documents_count: docs.length, stale_index_count: 0 });
  if (pathName === "/api/documents") return reply(route, { documents: docs });
  for (const doc of docs) {
    if (pathName === `/api/documents/${doc.id}`) return reply(route, doc);
    if (pathName === `/api/documents/${doc.id}/original` || pathName.startsWith(`/api/documents/${doc.id}/pages/`)) {
      return route.fulfill({ status: 200, contentType: "image/svg+xml", body: svg(`${doc.name} ${pathName.includes("/pages/") ? "2ページ" : "原本"}`) });
    }
  }
  if (pathName === "/api/ask") {
    const question = String(request.postData());
    if (question.includes("曖昧")) return reply(route, {
      status: "needs_clarification", answer: "該当しそうな資料が複数あります。資料名や識別番号を指定して質問してください。",
      sources: docs.map((doc) => ({ chunk_id: `${doc.id}-chunk`, document_id: doc.id,
        document_name: doc.name, unit_id: doc.units[0].id, kind: doc.units[0].kind,
        page_number: doc.units[0].page_number, start_sec: null, end_sec: null,
        snippet: doc.units[0].corrected_text, original_url: `/api/documents/${doc.id}/original` })),
    });
    const doc = question.includes("PDF") ? pdfDoc : imageDoc;
    return reply(route, { status: "answered", answer: "架空展示の水温は17℃です。", sources: [{
      chunk_id: `${doc.id}-chunk`, document_id: doc.id, document_name: doc.name,
      unit_id: doc.units[0].id, kind: doc.units[0].kind, page_number: doc.units[0].page_number,
      start_sec: null, end_sec: null, snippet: doc.units[0].corrected_text,
      original_url: `/api/documents/${doc.id}/original`,
    }] });
  }
  throw new Error(`Unexpected FAKE API request: ${request.method()} ${pathName}`);
});

const zoomIn = () => page.getByRole("button", { name: "拡大", exact: true });
const preview = () => page.locator(".preview-viewport");
const geometry = () => preview().evaluate((element) => ({ client: element.clientWidth, scroll: element.scrollWidth, left: element.scrollLeft }));
const checkZoom = async (prefix) => {
  await zoomIn().click();
  await page.getByText("150%", { exact: true }).waitFor();
  assert.ok((await geometry()).scroll > (await geometry()).client + 20, `${prefix}: enlarged original must scroll horizontally`);
  await preview().focus();
  await page.keyboard.press("ArrowRight");
  await page.waitForTimeout(80);
  assert.ok((await geometry()).left > 0, `${prefix}: keyboard must pan enlarged original`);
  await preview().evaluate((element) => { element.scrollLeft = 25; });
  const startLeft = (await geometry()).left;
  const box = await preview().boundingBox();
  assert.ok(box, `${prefix}: preview must be visible for mouse panning`);
  await page.mouse.move(box.x + box.width * 0.7, box.y + 45);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.7 - 55, box.y + 45, { steps: 4 });
  await page.mouse.up();
  assert.ok((await geometry()).left > startLeft + 20, `${prefix}: dragging must pan enlarged original`);
  await capture(`${prefix}_zoom150_FAKE.png`);
  await page.getByRole("button", { name: "全体表示" }).click();
  await page.getByText("100%", { exact: true }).waitFor();
  assert.equal((await geometry()).left, 0, `${prefix}: reset must return to left edge`);
};

try {
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: /保存した資料/ }).click();
  await page.locator(".document-row").filter({ hasText: imageDoc.name }).click();
  await page.getByRole("button", { name: "確認・編集する" }).click();
  await page.getByRole("img", { name: `${imageDoc.name} の元画像` }).waitFor();
  await capture("image_review_1280_FAKE.png");
  await checkZoom("image_review_1280");
  await page.setViewportSize({ width: 1024, height: 768 });
  const columns = await page.locator(".review-grid").evaluate((element) => {
    const [original, text] = element.children;
    return { original: original.getBoundingClientRect().x, text: text.getBoundingClientRect().x };
  });
  assert.ok(columns.text > columns.original + 100, "1024px review must keep original and text side by side");
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1), false);
  await capture("image_review_1024_FAKE.png");
  await checkZoom("image_review_1024");

  await page.setViewportSize({ width: 1280, height: 800 });
  await page.locator(".nav-item").filter({ hasText: "質問する" }).click();
  await page.getByRole("textbox", { name: "質問" }).fill("PDFの水温は？");
  await page.getByRole("button", { name: "資料から調べる" }).click();
  await page.getByRole("heading", { name: "回答案" }).waitFor();
  await page.getByRole("button", { name: "元資料と比較する" }).click();
  await page.getByRole("heading", { name: "根拠を元資料で確認" }).waitFor();
  await page.getByText("2 / 2 ページ").waitFor();
  await page.getByRole("img", { name: `${pdfDoc.name} の2ページのプレビュー` }).waitFor();
  const compareColumns = await page.locator(".source-comparison-grid").evaluate((element) => {
    const [original, text] = element.children;
    return { original: original.getBoundingClientRect().x, text: text.getBoundingClientRect().x };
  });
  assert.ok(compareColumns.text > compareColumns.original + 100, "source comparison must keep evidence text beside PDF");
  await capture("pdf_compare_1280_FAKE.png");
  await checkZoom("pdf_compare_1280");
  await page.getByRole("button", { name: "前へ" }).click();
  await page.getByText("1 / 2 ページ").waitFor();
  await page.getByRole("button", { name: "次へ" }).click();
  await page.getByText("2 / 2 ページ").waitFor();
  await page.setViewportSize({ width: 1024, height: 768 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1), false);
  await capture("pdf_compare_1024_FAKE.png");
  await checkZoom("pdf_compare_1024");
  await page.getByRole("button", { name: "← 質問結果へ戻る" }).click();
  await page.getByRole("heading", { name: "回答案" }).waitFor();
  await page.getByRole("textbox", { name: "質問" }).fill("曖昧な質問");
  await page.getByRole("button", { name: "資料から調べる" }).click();
  await page.getByRole("heading", { name: "対象を確認してください" }).waitFor();
  assert.equal(await page.getByRole("heading", { name: "回答案" }).count(), 0);
  await page.getByRole("heading", { name: "候補資料" }).waitFor();
  assert.equal(await page.locator(".source-card").count(), 2);
  await capture("clarification_1024_FAKE.png");
  await page.getByRole("button", { name: "元資料で確認する" }).first().click();
  await page.getByRole("heading", { name: "候補の元資料を確認" }).waitFor();
  await page.getByRole("heading", { name: "候補資料の内容" }).waitFor();
  assert.equal(await page.getByRole("heading", { name: "回答に使った箇所" }).count(), 0);
  await capture("clarification_source_1024_FAKE.png");
  await page.getByRole("button", { name: "← 質問結果へ戻る" }).click();
  await page.getByRole("heading", { name: "候補資料" }).waitFor();
  assert.deepEqual(errors, [], `Browser page errors: ${errors.join(" | ")}`);
  console.log(JSON.stringify({ kind: "FAKE_API_PREVIEW_ZOOM", result: "PASS", viewports: ["1280x800", "1024x768"], checks: ["image_review_zoom", "keyboard_pan", "mouse_pan", "reset", "side_by_side_1024", "pdf_page_navigation", "pdf_source_comparison", "pdf_zoom", "source_return", "clarification_candidates_not_answer_evidence"] }, null, 2));
} finally {
  await browser.close();
}
