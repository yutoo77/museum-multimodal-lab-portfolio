// Copyright (C) 2026 yutoo77
// SPDX-License-Identifier: AGPL-3.0-only

import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

// Real browser + local models. Verify that 5175 proxies the isolated 8766 API before any write.
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const baseUrl = process.env.MML_FRONTEND_URL;
const output = path.join(root, "docs", "ui-review", "final");
const manifestOutput = path.join(root, "outputs", "ui-final-candidates");
const progressOnly = process.argv.includes("--progress-only");
const answerImageOnly = process.argv.includes("--answer-image-only");
const helpOnly = process.argv.includes("--help-only");
const selectionOnly = process.argv.includes("--selection-only");
if (process.env.MML_E2E_ALLOW_WRITE !== "1" || !/^http:\/\/127\.0\.0\.1:5175\/?$/.test(baseUrl ?? "")) {
  throw new Error("Final visual E2E requires an isolated 127.0.0.1:5175 and MML_E2E_ALLOW_WRITE=1.");
}
const edge = [process.env.EDGE_PATH, "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe", "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe"].find((candidate) => candidate && existsSync(candidate));
if (!edge) throw new Error("Microsoft Edge was not found");

await mkdir(output, { recursive: true });
await mkdir(manifestOutput, { recursive: true });
const browser = await chromium.launch({ executablePath: edge, headless: true });
const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, locale: "ja-JP" });
const page = await context.newPage();
const errors = [];
const mode = selectionOnly || helpOnly
  ? "real local UI/API, no model inference in this mode; fictional fixtures"
  : progressOnly ? "real local UI/API and image VLM; fictional image"
    : answerImageOnly ? "real local UI/API and answer LLM; fictional registered sources"
      : "real local UI/API, image VLM, embedding and answer LLM; previously registered fictional PDF/audio sources";
const evidence = { mode, baseUrl, browser: await browser.version(), screenshots: [], checks: [] };
page.on("pageerror", (error) => errors.push(error.message));
page.on("dialog", (dialog) => void dialog.accept());

const capture = async (name) => {
  await page.waitForTimeout(200);
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
  await page.mouse.move((page.viewportSize()?.width ?? 1280) - 5, 5);
  await page.waitForTimeout(160);
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
  const file = path.join(output, name);
  await page.screenshot({ path: file });
  evidence.screenshots.push(name);
};
const nav = (label) => page.getByRole("navigation", { name: "画面" }).getByRole("button", { name: label, exact: true }).click();
const findDocument = async (name) => {
  await nav("保存した資料");
  await page.getByRole("button", { name: new RegExp(name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")) }).click();
  await page.locator(".detail-summary h2").filter({ hasText: name }).waitFor();
};
const waitForJob = async (id) => {
  const deadline = Date.now() + 300000;
  while (Date.now() < deadline) {
    const response = await page.request.get(new URL(`/api/jobs/${id}`, baseUrl).toString());
    assert.equal(response.status(), 200);
    const job = await response.json();
    if (job.status === "completed") return job;
    if (job.status === "failed") throw new Error(`Real model job failed: ${job.error}`);
    await page.waitForTimeout(600);
  }
  throw new Error(`Job ${id} timed out`);
};
const askAndCompare = async ({ name, question, expected, format, screenshotPrefix }) => {
  await nav("質問する");
  await page.getByRole("textbox", { name: "質問" }).fill(question);
  const responsePromise = page.waitForResponse((response) => response.request().method() === "POST" && new URL(response.url()).pathname === "/api/ask", { timeout: 120000 });
  await page.getByRole("button", { name: "資料から調べる" }).click();
  const response = await responsePromise;
  assert.equal(response.status(), 200);
  const answer = await response.json();
  assert.equal(answer.status, "answered", `${format}: ${answer.answer}`);
  assert.match(answer.answer, expected);
  const card = page.locator(".source-card").filter({ hasText: name });
  await card.waitFor();
  const document = (await page.request.get(new URL("/api/documents", baseUrl).toString())).json();
  const selected = (await document).documents.find((item) => item.name === name);
  assert.ok(answer.sources.some((source) => source.document_id === selected?.id));
  if (format === "image" && screenshotPrefix === "12") {
    await page.waitForTimeout(280);
    const focused = path.join(output, "12b_answer_focused.png");
    await page.screenshot({ path: focused });
    evidence.screenshots.push("12b_answer_focused.png");
    const sourceAction = await card.getByRole("button", { name: "元資料と比較する" }).boundingBox();
    evidence.checks.push(`Image answer autofocus: source action viewport y=${sourceAction ? Math.round(sourceAction.y) : "missing"}`);
  }
  await capture(`${screenshotPrefix}_answer_${format}.png`);
  await card.getByRole("button", { name: "元資料と比較する" }).click();
  await page.locator(".source-comparison-grid").waitFor();
  await page.locator(".comparison-toolbar strong").filter({ hasText: name }).waitFor();
  if (format === "pdf") await page.locator(".pdf-page-preview").evaluate((image) => image.decode());
  await capture(`${screenshotPrefix}_source_comparison_${format}.png`);
  assert.ok((await page.locator(".comparison-text blockquote").innerText()).trim());
  await page.getByRole("button", { name: "← 質問結果へ戻る" }).click();
  await page.locator(".answer-card").waitFor();
  evidence.checks.push(`${format}: real local answer, matching document_id, original/evidence comparison, return to answer`);
  return answer;
};

try {
  const documentResponse = await page.request.get(new URL("/api/documents", baseUrl).toString());
  assert.equal(documentResponse.status(), 200);
  const initialDocuments = (await documentResponse.json()).documents;
  const isolatedResponse = await page.request.get("http://127.0.0.1:8766/api/documents");
  assert.equal(isolatedResponse.status(), 200, "The isolated backend on 8766 must be running");
  assert.deepEqual(
    initialDocuments.map((doc) => doc.id).sort(),
    (await isolatedResponse.json()).documents.map((doc) => doc.id).sort(),
    "Frontend 5175 must proxy the same isolated document set as backend 8766",
  );
  assert.ok(initialDocuments.some((doc) => doc.name.includes("phase2-ui-lumina-3665033.png")), "Expected the isolated fictional fixture, not the normal demo database");
  for (const token of ["lumina", "sorano", "stardust"]) assert.ok(initialDocuments.some((doc) => doc.name.includes(token) && doc.status === "indexed" && doc.original_available));
  if (answerImageOnly || helpOnly || selectionOnly) {
    await page.goto(baseUrl, { waitUntil: "networkidle" });
    if (selectionOnly) {
      await capture("01_import_empty.png");
      await page.locator('.dropzone input[type="file"]').setInputFiles({
        name: "fictional_sorano_guide.pdf",
        mimeType: "application/pdf",
        buffer: await readFile(path.join(root, "data", "demo", "fictional_sorano_guide.pdf")),
      });
      await capture("02_import_selected.png");
      evidence.checks.push("Empty and file-selected import layouts captured without uploading");
    } else if (helpOnly) {
      await page.getByRole("button", { name: "使い方を開く", exact: true }).click();
      await page.getByRole("heading", { name: "6つの操作" }).waitFor();
      await capture("22_help.png");
      evidence.checks.push("Help opened from the real app without obscuring the primary navigation");
    } else {
      const image = initialDocuments.find((doc) => doc.name.includes("lumina") && doc.name.includes("3665033"));
      assert.ok(image);
      await askAndCompare({ name: image.name, question: "識別番号 PX-3665033 の水温は何℃ですか？", expected: /19\s*℃/, format: "image", screenshotPrefix: "12" });
    }
  } else {
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.getByRole("heading", { name: "資料を取り込む" }).waitFor();
  await capture("01_import_empty.png");

  const runId = String(Date.now()).slice(-7);
  const name = `final-ui-fictional-${runId}.png`;
  const corrected = `架空展示「アオノ光環」の識別番号は UI-${runId} です。体験時間は 17 分です。これは画面確認用の自作架空データです。`;
  const imageData = await page.evaluate((id) => {
    const canvas = document.createElement("canvas");
    canvas.width = 1100; canvas.height = 560;
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("Canvas is unavailable");
    ctx.fillStyle = "#ffffff"; ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#17394b"; ctx.font = 'bold 54px "Yu Gothic", sans-serif';
    ctx.fillText("架空展示：アオノ光環", 60, 100);
    ctx.font = '44px "Yu Gothic", sans-serif';
    ctx.fillText(`識別番号 UI-${id}`, 60, 215);
    ctx.fillText("体験時間 17 分", 60, 325);
    ctx.font = '28px "Yu Gothic", sans-serif';
    ctx.fillText("この画像は画面確認用の架空データです", 60, 455);
    return canvas.toDataURL("image/png").split(",")[1];
  }, runId);
  await page.locator('.dropzone input[type="file"]').setInputFiles({ name, mimeType: "image/png", buffer: Buffer.from(imageData, "base64") });
  await capture("02_import_selected.png");
  const uploadResponse = page.waitForResponse((response) => response.request().method() === "POST" && new URL(response.url()).pathname === "/api/documents");
  await page.getByRole("button", { name: "資料を追加", exact: true }).click();
  assert.equal((await uploadResponse).status(), 201);
  await page.locator(".workflow-section h2").filter({ hasText: name }).waitFor();
  const extractResponse = page.waitForResponse((response) => response.request().method() === "POST" && /\/extract$/.test(new URL(response.url()).pathname));
  await page.getByRole("button", { name: "読み取りを開始" }).click();
  const queued = await extractResponse;
  assert.equal(queued.status(), 202);
  const { job_id: jobId } = await queued.json();
  await page.locator(".job-state").waitFor();
  await capture("03_extraction_progress.png");
  await nav("質問する");
  await page.locator(".topbar-job").click();
  await page.locator(".workflow-section h2").filter({ hasText: name }).waitFor();
  const jobBox = await page.locator(".job-state").boundingBox();
  assert.ok(jobBox && jobBox.y >= 65 && jobBox.y < 750, `Job link target outside viewport: ${JSON.stringify(jobBox)}`);
  await capture("04_extraction_progress_after_click.png");
  evidence.checks.push(`Processing link opened the correct ${name}; job state was at y=${Math.round(jobBox.y)} in the 1280x800 viewport`);
  await waitForJob(jobId);
  if (!progressOnly) {
  await page.getByRole("heading", { name: "元資料と照らし合わせる" }).waitFor({ timeout: 300000 });
  await capture("05_review_image.png");
  const editor = page.getByRole("textbox", { name: "確認・修正するテキスト" }).first();
  await editor.fill(corrected);
  const patchResponse = page.waitForResponse((response) => response.request().method() === "PATCH" && /\/api\/extracted-units\//.test(new URL(response.url()).pathname));
  await page.getByRole("button", { name: "修正を保存" }).first().click();
  assert.equal((await patchResponse).status(), 200);
  await page.getByText("修正を保存しました。知識への登録はまだ行われていません。").waitFor();
  await capture("05b_review_corrected_not_registered.png");
  await page.getByLabel("元資料と抽出内容を確認しました").check();
  const approveResponse = page.waitForResponse((response) => response.request().method() === "POST" && /\/approve$/.test(new URL(response.url()).pathname));
  await page.getByRole("button", { name: "知識に登録", exact: true }).click();
  const approval = await approveResponse;
  assert.equal(approval.status(), 202);
  await waitForJob((await approval.json()).job_id);
  await page.getByText("知識に登録済み", { exact: true }).first().waitFor({ timeout: 120000 });
  await capture("08_approved.png");
  evidence.checks.push("Image VLM extraction, human correction, review checkbox, local index approval all completed on isolated data");

  for (const [token, file] of [["sorano", "06_review_pdf.png"], ["stardust", "07_review_audio.png"]]) {
    const target = initialDocuments.find((doc) => doc.name.includes(token));
    assert.ok(target);
    await findDocument(target.name);
    await page.getByRole("button", { name: "確認・編集する" }).click();
    await page.locator(".review-grid").waitFor();
    await capture(file);
  }

  await nav("保存した資料");
  await page.locator(".document-list").waitFor();
  await capture("09_documents.png");
  await nav("質問する");
  await page.getByRole("textbox", { name: "質問" }).waitFor();
  await capture("10_question_empty.png");

  const pdf = initialDocuments.find((doc) => doc.name.includes("sorano"));
  const audio = initialDocuments.find((doc) => doc.name.includes("stardust"));
  const image = initialDocuments.find((doc) => doc.name.includes("lumina") && doc.name.includes("3665033"));
  assert.ok(pdf && audio && image);
  await askAndCompare({ name: image.name, question: "識別番号 PX-3665033 の水温は何℃ですか？", expected: /19\s*℃/, format: "image", screenshotPrefix: "12" });
  await askAndCompare({ name: pdf.name, question: "ソラノ光路の体験時間は何分ですか？", expected: /23\s*分/, format: "pdf", screenshotPrefix: "14" });
  await askAndCompare({ name: audio.name, question: "星くずドームの最終案内は何時何分ですか？", expected: /(3\s*時\s*40\s*分|15\s*時\s*40\s*分|15:40)/, format: "audio", screenshotPrefix: "15" });

  await page.getByRole("textbox", { name: "質問" }).fill("架空の展示ソラノ光路の入場料は何円ですか？");
  const heldResponse = page.waitForResponse((response) => response.request().method() === "POST" && new URL(response.url()).pathname === "/api/ask", { timeout: 120000 });
  await page.getByRole("button", { name: "資料から調べる" }).click();
  const held = await (await heldResponse).json();
  assert.equal(held.status, "held");
  await page.getByRole("heading", { name: "登録資料からは確認できません" }).waitFor();
  await capture("16_held.png");
  evidence.checks.push("Out-of-scope question held by real local RAG; no fabricated price shown");

  await nav("資料を取り込む");
  await page.getByRole("button", { name: "別の資料を追加" }).click();
  await page.locator('.dropzone input[type="file"]').setInputFiles({ name: "fictional-broken.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4\nnot a valid document") });
  const invalidResponse = page.waitForResponse((response) => response.request().method() === "POST" && new URL(response.url()).pathname === "/api/documents");
  await page.getByRole("button", { name: "資料を追加", exact: true }).click();
  assert.equal((await invalidResponse).status(), 400);
  await page.getByRole("button", { name: "ファイルを選び直す" }).waitFor();
  await capture("17_upload_error.png");
  await page.getByRole("button", { name: "ファイルを選び直す" }).click();
  await page.locator('.dropzone input[type="file"]').waitFor();
  evidence.checks.push("Broken PDF rejection displayed a choose-again recovery action");

  await page.setViewportSize({ width: 1024, height: 768 });
  await capture("18_import_1024.png");
  await nav("保存した資料");
  await capture("19_documents_1024.png");
  await nav("質問する");
  await page.getByRole("textbox", { name: "質問" }).fill("ソラノ光路の体験時間は何分ですか？");
  const smallResponse = page.waitForResponse((response) => response.request().method() === "POST" && new URL(response.url()).pathname === "/api/ask", { timeout: 120000 });
  await page.getByRole("button", { name: "資料から調べる" }).click();
  const smallAnswer = await (await smallResponse).json();
  assert.equal(smallAnswer.status, "answered");
  await page.locator(".source-card").filter({ hasText: pdf.name }).waitFor();
  await capture("20_answer_1024.png");
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1), false, "1024px horizontal overflow");
  await page.locator(".source-card").filter({ hasText: pdf.name }).getByRole("button", { name: "元資料と比較する" }).click();
  await page.locator(".source-comparison-grid").waitFor();
  await capture("21_comparison_1024.png");
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1), false, "1024px comparison overflow");
  evidence.checks.push("1024x768 answer and source comparison no horizontal overflow");
  }
  }

  assert.deepEqual(errors, [], `Browser page errors: ${errors.join(" / ")}`);
  const manifestName = selectionOnly ? "capture-selection-manifest.json" : helpOnly ? "capture-help-manifest.json" : answerImageOnly ? "capture-answer-image-manifest.json" : progressOnly ? "capture-progress-manifest.json" : "capture-manifest.json";
  await writeFile(path.join(manifestOutput, manifestName), JSON.stringify(evidence, null, 2), "utf8");
  console.log(JSON.stringify(evidence, null, 2));
} finally {
  await browser.close();
}
