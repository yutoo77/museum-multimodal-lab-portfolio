// Copyright (C) 2026 yutoo77
// SPDX-License-Identifier: AGPL-3.0-only

import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const output = path.join(root, "outputs", "ui-smoke");
const baseUrl = process.env.MML_FRONTEND_URL || "http://127.0.0.1:5174";
const edgeCandidates = [
  process.env.EDGE_PATH,
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
].filter(Boolean);
const executablePath = edgeCandidates.find((candidate) => existsSync(candidate));

if (!executablePath) {
  throw new Error("Edge 実行ファイルが見つかりません。EDGE_PATH を指定してください。");
}

await mkdir(output, { recursive: true });
const browser = await chromium.launch({ executablePath, headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 960 }, locale: "ja-JP" });
const pageErrors = [];
page.on("pageerror", (error) => pageErrors.push(error.message));
const result = { browser: await browser.version(), baseUrl, screenshots: [], checks: [] };
const nav = (label) => page.getByRole("navigation", { name: "画面" }).getByRole("button", { name: label, exact: true });
const screenshot = async (name) => {
  const target = path.join(output, name);
  await page.screenshot({ path: target, fullPage: true });
  result.screenshots.push(target);
};

try {
  const response = await page.goto(baseUrl, { waitUntil: "networkidle", timeout: 30000 });
  assert.equal(response?.status(), 200, "Frontend should return HTTP 200");
  await page.getByRole("heading", { name: "資料を取り込む" }).waitFor();
  await page.getByText("このPC内で処理").waitFor();
  await screenshot("01-import.png");
  result.checks.push("取り込み画面とヘルス表示");

  await nav("保存した資料").click();
  await page.getByRole("heading", { name: "保存した資料" }).waitFor();
  const rows = page.locator(".document-row");
  await rows.first().waitFor();
  result.documentRows = await rows.count();
  assert.ok(result.documentRows > 0, "Saved documents should be listed");
  await screenshot("02-documents.png");
  result.checks.push("保存資料一覧");

  const oldRow = rows.filter({ hasText: "旧版" });
  if (await oldRow.count()) {
    await oldRow.first().click();
    await page.getByText("旧版", { exact: true }).waitFor();
    await page.getByRole("button", { name: "抽出内容を見る" }).click();
    await page.getByText("この資料は旧版です。原本と抽出内容を閲覧できますが、検索には使われません。").waitFor();
    assert.equal(await page.getByRole("button", { name: "再解析する" }).count(), 0, "Old version must not expose reanalysis");
    result.checks.push("旧版の閲覧と再解析禁止");
    await nav("保存した資料").click();
  }

  const imageRow = rows.filter({ hasText: "fictional_lumina_table.png" });
  assert.ok(await imageRow.count(), "Expected fictional image fixture in saved documents");
  await imageRow.first().click();
  await page.locator(".document-detail .image-preview").waitFor();
  const imageLoaded = await page.locator(".document-detail .image-preview").evaluate((image) => image.complete && image.naturalWidth > 0);
  assert.ok(imageLoaded, "Original image preview should load from backend");
  await screenshot("03-image-detail.png");
  result.checks.push("画像資料の詳細と原本プレビュー");

  await nav("質問する").click();
  await page.getByRole("heading", { name: "資料に質問する" }).waitFor();
  // Two fictional Lumina exhibits deliberately have different temperatures.
  // Include the exhibit ID so this is a provenance check, not an ambiguous query.
  await page.getByRole("textbox", { name: "質問" }).fill("識別番号LM-482のルミナ水槽の水温は何℃ですか？");
  const started = Date.now();
  const askResponsePromise = page.waitForResponse(
    (response) => response.request().method() === "POST" && new URL(response.url()).pathname === "/api/ask",
    { timeout: 120000 },
  );
  await page.getByRole("button", { name: "資料から調べる" }).click();
  const askResponse = await askResponsePromise;
  assert.equal(askResponse.status(), 200, `Question API returned HTTP ${askResponse.status()}`);
  await page.locator(".answer-card").waitFor({ timeout: 10000 });
  result.askSeconds = Number(((Date.now() - started) / 1000).toFixed(2));
  const answer = (await page.locator(".answer-card").innerText()).trim();
  assert.match(answer, /(^|[^\d])17([^\d]|$)/, "Answer should contain the identified image's water temperature");
  const sources = page.locator(".source-card");
  assert.ok((await sources.count()) > 0, "Answer should display real source cards");
  assert.match(await sources.first().innerText(), /fictional_lumina_table\.png/, "Source should name the image fixture");
  assert.match(await sources.first().innerText(), /17\s*\|\s*℃|17\s*℃/, "Source should retain the temperature unit");
  const originalHref = await sources.first().getByRole("link", { name: /元資料を開く/ }).getAttribute("href");
  assert.ok(originalHref?.startsWith("/api/documents/") && originalHref.includes("/original"), "Source link must point to backend original");
  await screenshot("04-answer-evidence.png");
  result.answer = answer;
  result.sourceCount = await sources.count();
  result.checks.push("質問への回答・画像由来の根拠・原本リンク");

  await sources.first().getByRole("button", { name: "元資料と比較する" }).click();
  await page.getByRole("heading", { name: "根拠を元資料で確認" }).waitFor();
  await page.locator(".document-detail .image-preview").waitFor();
  await page.getByRole("heading", { name: "回答に使った箇所" }).waitFor();
  result.checks.push("根拠から元資料とテキストの比較へ移動");
  await page.getByRole("button", { name: "← 質問結果へ戻る" }).click();
  await page.getByRole("heading", { name: "回答案" }).waitFor();
  await nav("保存した資料").click();
  await page.getByRole("heading", { name: "保存した資料" }).waitFor();

  const audioRow = rows.filter({ hasText: "fictional_stardust_announcement.wav" });
  assert.ok(await audioRow.count(), "Expected fictional WAV fixture in saved documents");
  await audioRow.first().click();
  const audio = page.locator(".document-detail audio");
  await audio.waitFor();
  const audioSource = await audio.getAttribute("src");
  assert.ok(audioSource?.startsWith("/api/documents/") && audioSource.endsWith("/original"), "Audio controls must reference the stored original");
  assert.equal(await audio.evaluate((element) => element.controls), true, "Audio controls should be enabled");
  await page.locator(".document-detail .read-unit .inline-button").first().click();
  await page.getByRole("button", { name: /秒から再生/ }).waitFor();
  await screenshot("05-audio-detail.png");
  result.checks.push("WAV原本の再生コントロールと時刻移動導線（音声出力は未確認）");

  const pdfRow = rows.filter({ hasText: "fictional_sorano_guide.pdf" }).filter({ hasNotText: "旧版" });
  assert.ok(await pdfRow.count(), "Expected current fictional PDF fixture in saved documents");
  await pdfRow.first().click();
  const pdfImage = page.locator(".document-detail img.pdf-page-preview");
  await pdfImage.waitFor();
  await pdfImage.evaluate((image) => image.decode());
  assert.ok(await pdfImage.evaluate((image) => image.naturalWidth > 0), "PDF page preview should visibly load");
  const previewSource = await pdfImage.getAttribute("src");
  assert.ok(previewSource?.includes("/api/documents/") && previewSource.endsWith("/pages/1/preview"), "PDF preview should target the local page renderer");
  const previewResponse = await page.request.get(new URL(previewSource, baseUrl).toString());
  assert.equal(previewResponse.status(), 200, "PDF page image should load");
  assert.match(previewResponse.headers()["content-type"] ?? "", /image\/png/, "Page preview should have PNG MIME type");
  assert.equal((await previewResponse.body()).subarray(0, 8).toString("hex"), "89504e470d0a1a0a", "Page preview should contain actual PNG bytes");
  const pdfSource = await page.locator(".document-detail .original-panel").getByRole("link", { name: /元PDFを開く/ }).getAttribute("href");
  assert.ok(pdfSource?.includes("/api/documents/") && pdfSource.includes("/original#page=1"), "Original PDF link should target page 1");
  const pdfResponse = await page.request.get(new URL(pdfSource.split("#")[0], baseUrl).toString());
  assert.equal(pdfResponse.status(), 200, "PDF original should load");
  assert.match(pdfResponse.headers()["content-type"] ?? "", /application\/pdf/, "PDF response should have PDF MIME type");
  assert.equal((await pdfResponse.body()).subarray(0, 5).toString(), "%PDF-", "PDF response should contain actual PDF bytes");
  await screenshot("06-pdf-detail.png");
  result.checks.push("現行PDFの描画画像・原本ページリンク・PDF実バイト");

  assert.deepEqual(pageErrors, [], `Browser runtime errors: ${pageErrors.join(" / ")}`);
  await writeFile(path.join(output, "result.json"), JSON.stringify(result, null, 2), "utf8");
  console.log(JSON.stringify(result, null, 2));
} finally {
  await browser.close();
}
