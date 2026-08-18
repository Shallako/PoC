/**
 * Drives the real wizard in a real browser.
 *
 * The Python suite proves the API; it cannot prove that a textarea keeps its
 * text and its caret when the page redraws underneath it, which is exactly what
 * the last few UI fixes claim. This does.
 *
 *   node check.mjs http://127.0.0.1:8810 <project-id>
 */
import { chromium } from "playwright";

const [baseUrl, projectId] = process.argv.slice(2);
if (!baseUrl || !projectId) {
  console.error("usage: node check.mjs <base-url> <project-id>");
  process.exit(2);
}

const failures = [];
const noise = [];

function check(name, ok, detail = "") {
  console.log(`${ok ? "  ok  " : "FAIL  "}${name}${detail ? "  -- " + detail : ""}`);
  if (!ok) failures.push(name);
}

const browser = await chromium.launch();
const page = await browser.newPage();

// A CSP violation or an uncaught error surfaces here and nowhere else. This is
// the half of the check that no amount of reading the source can replace.
page.on("pageerror", (e) => noise.push(`pageerror: ${e.message}`));
page.on("console", (m) => {
  if (m.type() === "error") noise.push(`console: ${m.text()}`);
});

await page.goto(baseUrl, { waitUntil: "networkidle" });
// `P` is declared with let, so it lives in the global lexical scope and is not
// a property of window -- reachable by bare name, not as window.P.
await page.waitForFunction(
  () => typeof P !== "undefined" && P && P.id, null, { timeout: 15000 });

// Load the seeded project rather than whatever happened to be first.
await page.selectOption("#projectPicker", projectId);
await page.waitForFunction(
  (id) => typeof P !== "undefined" && P && P.id === id, projectId,
  { timeout: 15000 });

// ---------------------------------------------------------------- step 1
const STORY = "A courier drives to the coast at dusk and does not stop.";
await page.fill("#story", STORY);
await page.fill("#sceneCount", "7");

// Exactly what every settings control does after its PATCH.
await page.evaluate(() => redraw());
check("step 1: the story survives a settings redraw",
      (await page.inputValue("#story")) === STORY);
check("step 1: the image count survives too",
      (await page.inputValue("#sceneCount")) === "7");

// ---------------------------------------------------------------- step 2
await page.evaluate(() => goStep(2));
const body = page.locator('textarea[data-scene="2"][data-field="body"]');
await body.waitFor({ state: "visible", timeout: 10000 });

const EDIT = "A courier at a steel sink, paper curling into flame.";
await body.fill(EDIT);
await body.click();
await page.evaluate(() => {
  const el = document.querySelector('textarea[data-scene="2"][data-field="body"]');
  el.focus();
  el.setSelectionRange(9, 9);          // caret mid-word, as if still typing
});

// Exactly what the render poller does every 2.5 seconds.
await page.evaluate(() => renderScenes());

const after = await page.evaluate(() => {
  const el = document.querySelector('textarea[data-scene="2"][data-field="body"]');
  return {
    value: el.value,
    focused: document.activeElement === el,
    caret: el.selectionStart,
  };
});
check("step 2: the edit survives a poll redraw", after.value === EDIT,
      after.value === EDIT ? "" : `got ${JSON.stringify(after.value)}`);
check("step 2: focus stays in the textarea", after.focused);
check("step 2: the caret does not jump to the end", after.caret === 9,
      `caret at ${after.caret}`);

// The style block is the same class of field, one level up.
const STYLE = "Cel-shaded, colder palette, no lettering.";
await page.fill("#styleProfile", STYLE);
await page.evaluate(() => renderScenes());
check("step 2: the style block survives too",
      (await page.inputValue("#styleProfile")) === STYLE);

// ---------------------------------------------------------------- cancels
for (const [id, label] of [["cancelSegmentBtn", "segment"],
                           ["cancelNarrBtn", "narration"]]) {
  const present = await page.locator(`#${id}`).count();
  const hidden = await page.locator(`#${id}`).evaluate(
    (el) => el.classList.contains("hidden"));
  check(`cancel: the ${label} button exists and is hidden when idle`,
        present === 1 && hidden);
}

// ---------------------------------------------------------------- the page itself
const nonce = await page.evaluate(() =>
  document.querySelector("script[nonce]") ? "yes" : "no");
check("csp: the page script carries a nonce", nonce === "yes");
check("no console errors or CSP violations", noise.length === 0,
      noise.slice(0, 3).join(" | "));

await browser.close();

console.log(failures.length
  ? `\n${failures.length} FAILED: ${failures.join(", ")}`
  : "\nall checks passed");
process.exit(failures.length ? 1 : 0);
