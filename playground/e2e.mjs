// End-to-end check of the built playground in a real browser.
//
//   node playground/e2e.mjs [--file] [--live] [--shots DIR] [--dark]
//
// Needs Playwright with Chromium: an installed `playwright` package, or PW_MODULE set to the
// path of another copy's index.mjs.
//
// Without --live it never sends a model request: every built-in example must replay from the
// recorded lockfile. --live reads OPENROUTER_API_KEY and asks one new question through the page.
import http from "node:http";
import { readFileSync, mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const page_path = path.join(here, "ex-regex-playground.html");
const args = process.argv.slice(2);
const useFile = args.includes("--file");
const live = args.includes("--live");
const dark = args.includes("--dark");
const shots = args.includes("--shots") ? args[args.indexOf("--shots") + 1] : null;
if (shots) mkdirSync(shots, { recursive: true });

const presets = JSON.parse(readFileSync(path.join(here, "presets.json"), "utf8"));
const { chromium } = await import(process.env.PW_MODULE || "playwright");
let server, url;
if (useFile) url = pathToFileURL(page_path).href;
else {
  server = http.createServer((q, r) => { r.writeHead(200, { "content-type": "text/html; charset=utf-8" }); r.end(readFileSync(page_path)); });
  await new Promise((res) => server.listen(0, "127.0.0.1", res));
  url = `http://127.0.0.1:${server.address().port}/`;
}
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, colorScheme: dark ? "dark" : "light" });
const page = await context.newPage();
const problems = [];
const decisions = [];
page.on("pageerror", (e) => problems.push("pageerror: " + e.message));
page.on("console", (m) => { if (m.type() === "error") problems.push("console: " + m.text()); });
context.on("request", (r) => { if (r.url().includes("openrouter.ai")) decisions.push(r.url()); });
page.on("worker", (w) => w.on("console", (m) => { if (m.type() === "error") problems.push("worker: " + m.text()); }));

const t0 = Date.now();
await page.goto(url);
await page.waitForFunction(() => /Recorded answers|Live Jev/.test(document.querySelector("#engine-label")?.textContent || "") , null, { timeout: 240000 });
console.log(`ready in ${((Date.now() - t0) / 1000).toFixed(1)} s`);

const results = [];
const check = (name, ok, detail = "") => { results.push([name, ok, detail]); console.log(`${ok ? "ok  " : "FAIL"} ${name}${detail ? "  " + detail : ""}`); };
const resultText = () => page.locator("#result").innerText();
const settle = async () => {
  await page.waitForFunction(() => {
    const r = document.querySelector("#result");
    return r && !/Asking|Getting ready/.test(r.querySelector("h2")?.textContent || "");
  }, null, { timeout: 60000 });
};

for (const [station, items] of Object.entries(presets)) {
  await page.locator(`[data-station="${station}"]`).click();
  for (let i = 0; i < items.length; i++) {
    await page.locator(`[data-preset="${i}"]`).click();
    await settle();
    // Units answers without a model, so it never shows "Asking": wait for its count.
    if (station === "units") await page.waitForFunction(() => /span/.test(document.querySelector("#result h2")?.textContent || ""), null, { timeout: 15000 });
    const text = await resultText();
    const source = station === "units" ? /No model call/.test(text) : /Recorded Jev answer/.test(text);
    const bad = /Not in the recorded answers|Error|Traceback/.test(text.split("\n")[0]);
    check(`${station}: ${items[i].title}`, source && !bad, source ? "" : text.slice(0, 160).replace(/\s+/g, " "));
    const code = await page.locator("#code pre").innerText().catch(() => "");
    if (/&(#\d+|amp|quot|lt|gt);/.test(code)) check(`${station}: ${items[i].title}: Python code shows no HTML entities`, false, code.match(/.{0,30}&(#\d+|amp|quot|lt|gt);.{0,10}/)[0]);
    if (shots && i === 0) await page.waitForTimeout(700), await page.screenshot({ path: path.join(shots, `${station}${dark ? "-dark" : ""}.png`), fullPage: true });
  }
}

// Units answer while you type, with no model.
await page.locator('[data-station="units"]').click();
await page.locator('[data-preset="0"]').click();
await settle();
await page.locator("#f-text").fill("Write to orbit.ferret@example.invalid or call 212-555-0187 before 2026-12-01.");
await page.waitForFunction(() => /3 spans/.test(document.querySelector("#result h2")?.textContent || ""), null, { timeout: 15000 }).then(
  () => check("units: live typing finds three spans", true),
  async () => check("units: live typing finds three spans", false, (await resultText()).slice(0, 120)),
);

// The scan threshold re-thresholds without asking again.
await page.locator('[data-station="scan"]').click();
await settle();
const before = await page.locator(".span-row.on").count();
await page.locator("#scan-t").fill("0.99");
const after = await page.locator(".span-row.on").count();
check("scan: slider moves the threshold locally", before > after, `${before} -> ${after}`);

// A new question is not recorded: the page says so, and the stand-in runs.
await page.locator('[data-station="test"]').click();
await settle();
await page.locator("#f-meaning").fill("mentions a penguin");
await page.locator("#run").click();
await settle();
check("test: an unrecorded question is named", /Not in the recorded answers/.test(await resultText()));
if (!live) {
  await page.locator('[data-act="standin"]').click();
  await settle();
  check("test: the keyword stand-in answers", /Keyword stand-in/.test(await resultText()));
}

// The engine station: stats, lockfile, typed errors.
await page.locator('[data-station="engine"]').click();
await page.locator(".stat").first().waitFor();
const replayed = Number(await page.locator(".stat strong").nth(1).innerText());
check("engine: replayed decisions are counted", replayed > 30, String(replayed));
for (const [kind, name] of [["budget", "BudgetExceeded"], ["cachemiss", "CacheMiss"], ["config", "ConfigError"], ["limit", "LimitError"], ["value", "ValueError"]]) {
  await page.locator(`[data-err="${kind}"]`).click();
  await page.waitForFunction((k) => (document.querySelector("#err-" + k)?.textContent || "").length > 0, kind, { timeout: 15000 });
  const said = await page.locator("#err-" + kind).innerText();
  check(`engine: ${name}`, said.startsWith(name), said.slice(0, 100));
}
if (shots) await page.screenshot({ path: path.join(shots, `engine${dark ? "-dark" : ""}.png`), fullPage: true });

// The Python console.
await page.locator('[data-station="console"]').click();
// Every snippet runs offline: its questions are in the lockfile.
const snippets = JSON.parse(readFileSync(path.join(here, "snippets.json"), "utf8"));
for (let i = 0; i < snippets.length; i++) {
  await page.locator(`[data-snippet="${i}"]`).click();
  const out = await page.waitForFunction(() => {
    const t = document.querySelector("#console-out")?.textContent || "";
    return t && t !== "Running..." ? t : null;
  }, null, { timeout: 20000 }).then((h) => h.jsonValue(), () => "(no output)");
  const empty = /^\s*(\[\]|\(\)|\{\}|None)\s*$/.test(out);
  check(`console: ${snippets[i].title}`, out !== "(no output)" && !empty && !/Traceback|not in the recorded answers/.test(out), out.slice(0, 160).replace(/\s+/g, " "));
}
// An unrecorded question explains itself instead of printing a traceback.
await page.locator("#console-code").fill('ex.test("mentions a penguin", "A small emperor chick named Pebble.", engine=engine)');
await page.locator("#run").click();
await page.waitForFunction(() => !/^Running/.test(document.querySelector("#console-out")?.textContent || "Running"), null, { timeout: 20000 });
const missed = await page.locator("#console-out").innerText();
check("console: an unrecorded question is explained", /not in the recorded answers/.test(missed) && !/Traceback/.test(missed), missed.slice(0, 120));
if (shots) await page.screenshot({ path: path.join(shots, `console${dark ? "-dark" : ""}.png`), fullPage: true });

if (live) {
  const key = process.env.OPENROUTER_API_KEY;
  await page.locator("#engine-pill").click();
  await page.locator("#key-input").fill(key);
  await page.locator("#budget-input").fill("0.02");
  await page.getByRole("button", { name: "Use this key" }).click();
  await page.waitForFunction(() => /Live Jev/.test(document.querySelector("#engine-label")?.textContent || ""), null, { timeout: 15000 });
  await page.locator('[data-station="test"]').click();
  await settle();
  await page.locator("#f-meaning").fill("mentions a penguin");
  await page.locator("#f-text").fill("The zoo's new arrival is a small emperor chick named Pebble.");
  await page.locator("#run").click();
  await settle();
  const text = await resultText();
  check("live: a new question goes to Jev", /Live Jev/.test(text), text.split("\n").slice(0, 3).join(" | "));
}

check(live ? "network: requests went to OpenRouter only when live" : "network: no model request was sent", live ? decisions.length >= 1 : decisions.length === 0, `${decisions.length} request(s)`);
check("no page or console errors", problems.length === 0, problems.slice(0, 5).join(" / "));
await browser.close();
server?.close();
const failed = results.filter((r) => !r[1]).length;
console.log(`${results.length - failed} of ${results.length} checks passed`);
process.exit(failed ? 1 : 0);
