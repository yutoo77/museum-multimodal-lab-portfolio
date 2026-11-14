// Copyright (C) 2026 yutoo77
// SPDX-License-Identifier: AGPL-3.0-only

// Browser regression for UI micro-polish. All API responses are synthetic and intercepted.
// No model call or persistent museum data is used.
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const baseUrl = process.env.MML_FRONTEND_URL ?? "http://127.0.0.1:5174/";
if (!/^http:\/\/127\.0\.0\.1:517[45]\/?$/.test(baseUrl)) {
  throw new Error("micro-polish:ui is limited to the local frontend on port 5174 or 5175");
}
const edgeCandidates = [
  process.env.EDGE_PATH,
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
].filter(Boolean);
const executablePath = edgeCandidates.find((candidate) => existsSync(candidate));
if (!executablePath) throw new Error("Microsoft Edge not found");

const shortSnippet = "架空の水槽には標識があります。";
const longSnippet = [
  shortSnippet,
  ...Array.from({ length: 28 }, (_, index) =>
    `第${index + 1}観察欄：架空の水槽では、時刻と水温を確認してから説明します。`
  ),
].join("\n");
const doc = {
  id: "synthetic-micro-document", name: "架空の水槽図.png", mime_type: "image/png",
  status: "indexed", created_at: "2026-11-09T10:00:00Z", updated_at: "2026-11-09T10:00:00Z",
  version: 1, extraction_model: "synthetic-model", original_available: true,
  reindex_required: false, review_history: [],
  units: [{
    id: "synthetic-micro-unit", document_id: "synthetic-micro-document",
    kind: "image_interpretation", extracted_text: longSnippet,
    corrected_text: longSnippet, status: "reviewed",
    page_number: null, start_sec: null, end_sec: null, model_name: "synthetic-model",
  }],
};
const png = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL/nwAAAABJRU5ErkJggg==",
  "base64",
);
const reply = (route, body, status = 200) => route.fulfill({
  status, contentType: "application/json", body: JSON.stringify(body),
});
const source = (snippet) => ({
  chunk_id: "synthetic-micro-chunk", document_id: doc.id, document_name: doc.name,
  unit_id: doc.units[0].id, kind: "image_interpretation", page_number: null,
  start_sec: null, end_sec: null, snippet,
  original_url: `/api/documents/${doc.id}/original`,
});

const browser = await chromium.launch({ executablePath, headless: true });
const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, locale: "ja-JP" });
const page = await context.newPage();
const errors = [];
const requests = [];
const captures = [];
page.on("pageerror", (error) => errors.push(error.message));
let releaseSlowAnswer;
let signalSlowRoute;
const slowRouteReady = new Promise((resolve) => { signalSlowRoute = resolve; });
const capture = async (name) => {
  if (process.env.MML_CAPTURE_POLISH_UI !== "1") return;
  const directory = new URL("../../docs/ui-review/final-polish/", import.meta.url);
  await mkdir(directory, { recursive: true });
  await page.mouse.move(1, 1);
  await page.screenshot({ path: fileURLToPath(new URL(name, directory)), fullPage: false });
  captures.push(name);
};

await page.route("**/api/**", async (route) => {
  const request = route.request();
  const path = new URL(request.url()).pathname;
  const method = request.method();
  requests.push(`${method} ${path}`);
  if (path === "/api/health" && method === "GET") return reply(route, {
    status: "ok", ollama: { available: true, models: [] },
    models: { answer: "synthetic", embedding: "synthetic", vision: "synthetic", asr: "synthetic" },
    documents_count: 1, stale_index_count: 0,
  });
  if (path === "/api/documents" && method === "GET") return reply(route, { documents: [doc] });
  if (path === `/api/documents/${doc.id}` && method === "GET") return reply(route, doc);
  if (path === `/api/documents/${doc.id}/original` && method === "GET") {
    return route.fulfill({ status: 200, contentType: "image/png", body: png });
  }
  if (path === "/api/ask" && method === "POST") {
    const question = JSON.parse(request.postData()).question;
    if (question === "遅い回答を調べる") {
      await new Promise((resolve) => {
        releaseSlowAnswer = resolve;
        signalSlowRoute();
      });
      return reply(route, { status: "answered", answer: "遅い回答が届きました。", sources: [source(shortSnippet)] });
    }
    if (question === "資料にない質問") {
      return reply(route, { status: "held", answer: "登録資料からは確認できません。", sources: [] });
    }
    const long = question === "長い根拠を調べる";
    return reply(route, {
      status: "answered", answer: long ? "長い根拠の回答案です。" : "短い根拠の回答案です。",
      sources: [source(long ? longSnippet : shortSnippet)],
    });
  }
  throw new Error(`Unexpected synthetic API request: ${method} ${path}`);
});

const question = page.getByRole("textbox", { name: "質問" });
const ask = () => page.getByRole("button", { name: "資料から調べる" }).click();
const waitAnswer = () => page.locator(".answer-section").waitFor({ state: "visible" });
const noAnswer = async () => {
  assert.equal(await page.locator(".answer-section").count(), 0, "Old answer must be removed after question edit");
};

try {
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: /質問する/ }).click();
  await question.fill("短い根拠を調べる");
  await ask();
  await waitAnswer();
  await page.getByText("短い根拠の回答案です。", { exact: true }).waitFor();
  assert.equal(await page.locator(".source-card blockquote").textContent(), shortSnippet);
  assert.equal(await page.getByRole("button", { name: "全文を見る" }).count(), 0,
    "Short source should remain fully visible without a disclosure button");

  await question.fill("長い根拠を調べる");
  await noAnswer();
  await ask();
  await waitAnswer();
  await page.getByText("長い根拠の回答案です。", { exact: true }).waitFor();
  const quote = page.locator(".source-card blockquote");
  assert.equal(await quote.textContent(), longSnippet, "The full source text must remain in the DOM");
  const collapsedHeight = (await quote.boundingBox())?.height;
  assert.ok(collapsedHeight && collapsedHeight < 150, `Long source should start visually collapsed: ${collapsedHeight}`);
  await page.locator(".source-card").scrollIntoViewIfNeeded();
  await capture("07_long_source_collapsed_FAKE.png");
  const expand = page.getByRole("button", { name: "全文を見る" });
  await expand.focus();
  await page.keyboard.press("Enter");
  await page.getByRole("button", { name: "閉じる" }).waitFor();
  const expandedHeight = (await quote.boundingBox())?.height;
  assert.ok(expandedHeight && expandedHeight > collapsedHeight + 100,
    `Expanded source should visibly show more text: ${collapsedHeight} -> ${expandedHeight}`);
  await capture("08_long_source_expanded_FAKE.png");
  await page.getByRole("button", { name: "閉じる" }).click();
  await page.getByRole("button", { name: "全文を見る" }).waitFor();
  assert.equal(await quote.textContent(), longSnippet, "Collapse must not alter source data");

  await page.getByRole("button", { name: "元資料と比較する" }).click();
  await page.getByRole("heading", { name: "根拠を元資料で確認" }).waitFor();
  await page.getByRole("img", { name: `${doc.name} の元画像` }).waitFor();
  await page.getByRole("button", { name: "← 質問結果へ戻る" }).click();
  await page.getByText("長い根拠の回答案です。", { exact: true }).waitFor();
  assert.equal(await question.inputValue(), "長い根拠を調べる", "Comparison return should preserve the question");

  await question.fill("資料にない質問");
  await noAnswer();
  await question.scrollIntoViewIfNeeded();
  await capture("09_question_edited_old_answer_cleared_FAKE.png");
  await ask();
  await page.getByRole("heading", { name: "登録資料からは確認できません" }).waitFor();
  await question.fill("遅い回答を調べる");
  await noAnswer();
  await ask();
  let slowTimer;
  await Promise.race([
    slowRouteReady,
    new Promise((_, reject) => {
      slowTimer = setTimeout(() => reject(new Error("Synthetic delayed answer was not requested")), 10000);
    }),
  ]);
  clearTimeout(slowTimer);
  await page.waitForFunction(() => document.querySelector("textarea#question")?.disabled === true);
  assert.equal(await question.isDisabled(), true, "Question editing must be blocked during request");
  assert.equal(await page.getByRole("button", { name: "調査中…" }).isDisabled(), true,
    "Submitting another question must be blocked during request");
  assert.equal(requests.filter((entry) => entry === "POST /api/ask").length, 4,
    "A delayed request must not start an accidental duplicate ask");
  releaseSlowAnswer();
  await page.getByText("遅い回答が届きました。", { exact: true }).waitFor();
  assert.equal(await question.isDisabled(), false, "Question must become editable after response");
  await question.fill("短い根拠を調べる");
  await noAnswer();

  assert.deepEqual(errors, [], `Browser runtime errors: ${errors.join(" | ")}`);
  console.log(JSON.stringify({
    kind: "FAKE_API_MICRO_POLISH", result: "PASS",
    checks: ["answer_clear_on_edit", "held_clear_on_edit", "short_source", "long_source_keyboard_disclosure",
      "source_comparison_return", "inflight_edit_and_double_submit_blocked"],
    ...(captures.length ? { screenshots: captures } : {}),
  }, null, 2));
} finally {
  releaseSlowAnswer?.();
  await browser.close();
}
