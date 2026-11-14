// Copyright (C) 2026 yutoo77
// SPDX-License-Identifier: AGPL-3.0-only

import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const output = process.env.MML_UI_OUTPUT_DIR
  ? path.resolve(process.env.MML_UI_OUTPUT_DIR)
  : path.join(root, "outputs", "phase2-ui", "after");
const baseUrl = process.env.MML_FRONTEND_URL;
if (process.env.MML_E2E_ALLOW_WRITE !== "1" || !baseUrl || !/^http:\/\/127\.0\.0\.1:5175\/?$/.test(baseUrl)) {
  throw new Error("UI書き込みテストは独立した 127.0.0.1:5175 でのみ実行できます。MML_E2E_ALLOW_WRITE=1 と MML_FRONTEND_URL を設定してください。");
}

const edgeCandidates = [
  process.env.EDGE_PATH,
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
].filter(Boolean);
const executablePath = edgeCandidates.find((candidate) => existsSync(candidate));
if (!executablePath) throw new Error("Edge 実行ファイルが見つかりません");

const runId = String(Date.now()).slice(-7);
const imageId = `PX-${runId}`;
const cases = [
  {
    format: "image",
    name: `phase2-ui-lumina-${runId}.png`,
    type: "image/png",
    corrected: `架空展示「ミオラ光台」の識別番号は ${imageId} です。水温は 19℃、流量は 37 L/分です。これらは自作の架空データです。`,
    question: `識別番号 ${imageId} の水温は何℃ですか？`,
    expected: /19\s*℃/,
  },
  {
    format: "pdf",
    name: `phase2-ui-sorano-${runId}.pdf`,
    type: "application/pdf",
    corrected: "架空デモ資料：ソラノ光路。この展示は実在の科学館の資料ではありません。体験時間は23分です。展示に使う光の色は青です。ルミナ水槽とは別の展示です。",
    question: "ソラノ光路の体験時間は何分ですか？",
    expected: /23\s*分/,
  },
  {
    format: "audio",
    name: `phase2-ui-stardust-${runId}.wav`,
    type: "audio/wav",
    corrected: "架空の展示、星くずドームの最終案内は午後3時40分です。",
    question: "星くずドームの最終案内は何時何分ですか？",
    expected: /(3\s*時\s*40\s*分|15\s*時\s*40\s*分|15:40)/,
  },
];

await mkdir(output, { recursive: true });
const browser = await chromium.launch({ executablePath, headless: true, args: ["--autoplay-policy=no-user-gesture-required"] });
const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, locale: "ja-JP" });
const page = await context.newPage();
const pageErrors = [];
page.on("pageerror", (error) => pageErrors.push(error.message));
page.on("dialog", (dialog) => void dialog.accept());
const result = { browser: await browser.version(), baseUrl, runId, cases: [], screenshots: [], checks: [] };
const nav = (label) => page.getByRole("navigation", { name: "画面" }).getByRole("button", { name: label, exact: true });

const screenshot = async (name) => {
  const target = path.join(output, name);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.mouse.move((page.viewportSize()?.width ?? 1280) - 20, 20);
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await page.screenshot({ path: target, fullPage: true });
  result.screenshots.push(target);
};
const awaitJob = async (jobId, timeout = 300000) => {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const response = await page.request.get(new URL(`/api/jobs/${jobId}`, baseUrl).toString());
    assert.equal(response.status(), 200, `Could not read job ${jobId}`);
    const job = await response.json();
    if (job.status === "completed") return job;
    if (job.status === "failed") throw new Error(`Job ${jobId} failed: ${job.error}`);
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  throw new Error(`Job ${jobId} timed out`);
};
const createImage = async () => {
  const base64 = await page.evaluate((id) => {
    const canvas = document.createElement("canvas");
    canvas.width = 1200;
    canvas.height = 620;
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("Canvas is unavailable");
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, 1200, 620);
    ctx.fillStyle = "#17394b";
    ctx.font = 'bold 58px "Yu Gothic", sans-serif';
    ctx.fillText("架空展示：ミオラ光台", 65, 100);
    ctx.font = '46px "Yu Gothic", sans-serif';
    ctx.fillText(`識別番号 ${id}`, 65, 215);
    ctx.fillText("水温 19℃", 65, 325);
    ctx.fillText("流量 37 L/分", 65, 435);
    ctx.font = '28px "Yu Gothic", sans-serif';
    ctx.fillText("※この画像はテスト用の架空データです", 65, 550);
    return canvas.toDataURL("image/png").split(",")[1];
  }, imageId);
  return Buffer.from(base64, "base64");
};
const inputFor = async (item) => {
  if (item.format === "image") return await createImage();
  const fixture = item.format === "pdf" ? "fictional_sorano_guide.pdf" : "fictional_stardust_announcement.wav";
  return await readFile(path.join(root, "data", "demo", fixture));
};
const upload = async (item) => {
  await nav("資料を取り込む").click();
  if (await page.locator('.dropzone input[type="file"]').count() === 0) {
    await page.getByRole("button", { name: "別の資料を追加" }).click();
  }
  await page.locator('.dropzone input[type="file"]').setInputFiles({ name: item.name, mimeType: item.type, buffer: await inputFor(item) });
  const [response] = await Promise.all([
    page.waitForResponse((response) => response.request().method() === "POST" && new URL(response.url()).pathname === "/api/documents"),
    page.getByRole("button", { name: "資料を追加", exact: true }).click(),
  ]);
  assert.equal(response.status(), 201, `${item.format} upload failed`);
  const document = await response.json();
  assert.equal(document.name, item.name);
  await page.locator(".workflow-section h2").filter({ hasText: item.name }).waitFor();
  return document.id;
};
const extract = async (item) => {
  const [response] = await Promise.all([
    page.waitForResponse((response) => response.request().method() === "POST" && /\/extract$/.test(new URL(response.url()).pathname)),
    page.getByRole("button", { name: "読み取りを開始" }).click(),
  ]);
  assert.equal(response.status(), 202, `${item.format} extract did not queue`);
  const { job_id: jobId } = await response.json();
  await page.locator(".job-state").waitFor({ timeout: 10000 });
  assert.doesNotMatch(await page.locator(".job-state").innerText(), /\d+\s*%/, "UI must not show an invented completion percentage");
  if (item.format === "image") {
    await screenshot("01-image-reading-1280.png");
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator(".workflow-section h2").filter({ hasText: item.name }).waitFor();
    await page.locator(".job-state").waitFor({ timeout: 10000 });
    result.checks.push("画像の長時間ジョブと再読み込み後の状態復元");
  }
  const job = await awaitJob(jobId);
  await page.getByRole("heading", { name: "元資料と照らし合わせる" }).waitFor({ timeout: 300000 });
  assert.notEqual(await page.locator(".workflow-section .status-pill").innerText(), "処理失敗");
  return job;
};
const approve = async (item) => {
  const editor = page.getByRole("textbox", { name: "確認・修正するテキスト" }).first();
  const raw = await editor.inputValue();
  await editor.fill(item.corrected);
  const [saved] = await Promise.all([
    page.waitForResponse((response) => response.request().method() === "PATCH" && /\/api\/extracted-units\//.test(new URL(response.url()).pathname)),
    page.getByRole("button", { name: "修正を保存" }).first().click(),
  ]);
  assert.equal(saved.status(), 200, `${item.format} correction not saved`);
  await page.getByText("修正を保存しました。知識への登録はまだ行われていません。").waitFor();
  await page.getByLabel("元資料と抽出内容を確認しました").check();
  const [response] = await Promise.all([
    page.waitForResponse((response) => response.request().method() === "POST" && /\/approve$/.test(new URL(response.url()).pathname)),
    page.getByRole("button", { name: "知識に登録", exact: true }).click(),
  ]);
  assert.equal(response.status(), 202, `${item.format} approval did not queue`);
  const { job_id: jobId } = await response.json();
  const job = await awaitJob(jobId, 120000);
  await page.locator(".workflow-section .status-pill").filter({ hasText: "検索に登録済み" }).waitFor({ timeout: 120000 });
  return { rawText: raw, job };
};
const ask = async (item, documentId) => {
  await nav("質問する").click();
  await page.getByRole("textbox", { name: "質問" }).fill(item.question);
  const started = Date.now();
  const [response] = await Promise.all([
    page.waitForResponse((response) => response.request().method() === "POST" && new URL(response.url()).pathname === "/api/ask", { timeout: 120000 }),
    page.getByRole("button", { name: "資料から調べる" }).click(),
  ]);
  assert.equal(response.status(), 200, `${item.format} ask returned HTTP ${response.status()}`);
  const data = await response.json();
  assert.equal(data.status, "answered", `${item.format} question was held: ${data.answer}`);
  assert.match(data.answer, item.expected, `${item.format} answer did not contain expected fictional value`);
  assert.ok(data.sources.some((source) => source.document_id === documentId), `${item.format} source did not include the newly registered document`);
  await page.locator(".answer-card").waitFor();
  const card = page.locator(".source-card").filter({ hasText: item.name });
  await card.waitFor();
  const href = await card.getByRole("link", { name: /元資料を開く/ }).getAttribute("href");
  assert.ok(href?.includes(`/api/documents/${documentId}/original`), `${item.format} source should lead to its real original`);
  const original = await page.request.get(new URL(href.split("#")[0], baseUrl).toString());
  assert.equal(original.status(), 200, `${item.format} original endpoint failed`);
  const body = await original.body();
  if (item.format === "image") assert.equal(body.subarray(0, 8).toString("hex"), "89504e470d0a1a0a");
  if (item.format === "pdf") assert.equal(body.subarray(0, 5).toString(), "%PDF-");
  if (item.format === "audio") assert.equal(body.subarray(0, 4).toString(), "RIFF");
  return { answer: data.answer, sourceCount: data.sources.length, seconds: Number(((Date.now() - started) / 1000).toFixed(2)), href };
};

try {
  const response = await page.goto(baseUrl, { waitUntil: "networkidle", timeout: 30000 });
  assert.equal(response?.status(), 200);
  await page.getByRole("heading", { name: "資料を取り込む" }).waitFor();
  await screenshot("00-import-empty-1280.png");

  for (const item of cases) {
    const documentId = await upload(item);
    const extractionJob = await extract(item);
    const { rawText, job: approvalJob } = await approve(item);
    if (item.format === "audio") {
      const player = page.locator(".workflow-section audio");
      await player.waitFor();
      await player.evaluate(async (element) => { element.muted = true; await element.play(); });
      await page.waitForTimeout(700);
      const playback = await player.evaluate((element) => ({ readyState: element.readyState, currentTime: element.currentTime, error: element.error?.message ?? null }));
      assert.equal(playback.error, null, "WAV browser decoder failed");
      assert.ok(playback.readyState >= 2 && playback.currentTime > 0, "WAV timeline did not advance");
      result.audioPlaybackMechanics = playback;
      await player.evaluate((element) => element.pause());
      result.checks.push("WAVのブラウザーでのデコードと再生時刻進行（実音声の聴取ではない）");
    }
    const answered = await ask(item, documentId);
    result.cases.push({ format: item.format, name: item.name, documentId, rawText, correctedText: item.corrected, extractSeconds: extractionJob.duration_sec ?? null, approveSeconds: approvalJob.duration_sec ?? null, ...answered });
    await screenshot(`${item.format}-answer-1280.png`);
    await page.locator(".source-card").filter({ hasText: item.name }).getByRole("button", { name: "元資料と比較する" }).click();
    await page.locator(".source-comparison-grid .original-panel").waitFor();
    await page.locator(".source-comparison-grid .comparison-text blockquote").waitFor();
    assert.ok((await page.locator(".source-comparison-grid .comparison-text blockquote").innerText()).trim(), "Selected evidence text must be visible");
    if (item.format === "pdf") {
      const pdfPreview = page.locator(".source-comparison-grid .pdf-page-preview");
      await pdfPreview.evaluate((image) => image.decode());
      assert.ok(await pdfPreview.evaluate((image) => image.naturalWidth > 0), "PDF preview failed");
    }
    if (item.format === "audio") {
      await page.getByRole("button", { name: /秒から再生/ }).waitFor();
    }
    await screenshot(`${item.format}-original-1280.png`);
    await page.getByRole("button", { name: "← 質問結果へ戻る" }).click();
    await page.locator(".answer-card").waitFor();
  }

  await page.setViewportSize({ width: 1024, height: 768 });
  for (const tab of ["import", "documents", "ask"]) {
    await nav(tab === "import" ? "資料を取り込む" : tab === "documents" ? "保存した資料" : "質問する").click();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
    assert.equal(overflow, false, `${tab} has horizontal overflow at 1024px`);
    await screenshot(`${tab}-1024.png`);
  }
  result.checks.push("1280×800 と 1024×768 の主要画面・横スクロールなし");
  assert.deepEqual(pageErrors, [], `Browser runtime errors: ${pageErrors.join(" / ")}`);
  await writeFile(path.join(output, "result.json"), JSON.stringify(result, null, 2), "utf8");
  console.log(JSON.stringify(result, null, 2));
} finally {
  await browser.close();
}
