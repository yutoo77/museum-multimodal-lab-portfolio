// Copyright (C) 2026 yutoo77
// SPDX-License-Identifier: AGPL-3.0-only

import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { mkdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const output = path.join(root, "outputs", "phase2-ui", "after");
const baseUrl = process.env.MML_FRONTEND_URL || "http://127.0.0.1:5174";
const edgeCandidates = [
  process.env.EDGE_PATH,
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
].filter(Boolean);
const executablePath = edgeCandidates.find((candidate) => existsSync(candidate));
if (!executablePath) throw new Error("Edge 実行ファイルが見つかりません");
if (!/^http:\/\/127\.0\.0\.1:\d+\/?$/.test(baseUrl)) throw new Error("local frontend only");

await mkdir(output, { recursive: true });
const browser = await chromium.launch({ executablePath, headless: true });
const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, locale: "ja-JP" });
const screens = [];
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
try {
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  for (const width of [1280, 1024]) {
    await page.setViewportSize({ width, height: width === 1280 ? 800 : 768 });
    for (const [name, label, heading] of [["import", "資料を取り込む", "資料を取り込む"], ["documents", "保存した資料", "保存した資料"], ["ask", "質問する", "資料に質問する"]]) {
      const tab = page.locator(".nav-item").filter({ hasText: label });
      await tab.click();
      await page.getByRole("heading", { name: heading, exact: true }).waitFor();
      await page.waitForFunction((label) => [...document.querySelectorAll(".nav-item")].some((item) => item.textContent?.includes(label) && item.getAttribute("aria-current") === "page"), label);
      assert.equal(await page.locator(".nav-item.active").count(), 1, `${name} should be the sole active navigation item`);
      assert.match(await page.locator(".nav-item.active").innerText(), new RegExp(label), `${name} active navigation does not match content`);
      await page.mouse.move(width - 20, 20);
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      await page.waitForTimeout(100);
      const target = path.join(output, `visual-${name}-${width}.png`);
      await page.screenshot({ path: target });
      screens.push(target);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1), false, `${name} at ${width}px overflows horizontally`);
    }
  }
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ baseUrl, screens, horizontalOverflow: false, pageErrors: errors }, null, 2));
} finally {
  await browser.close();
}
