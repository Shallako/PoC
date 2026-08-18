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
//
// Waiting on P.id alone is not enough and was intermittently wrong: boot opens
// the first project in the list, which is often this one, so P.id matches
// before the reload this selectOption just triggered has finished -- and that
// reload then refills the form over whatever the check typed. LOAD_SETTLED is
// what says no load is still in flight.
await page.selectOption("#projectPicker", projectId);
await page.waitForFunction(
  (id) => typeof P !== "undefined" && P && P.id === id
          && LOAD_SETTLED === LOAD_SEQ, projectId,
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

// ---------------------------------------------------------------- the limit
// The story limit belongs to config.py. The page used to repeat it in four
// places, so raising it gave a server that accepted the story and a counter
// that turned red on it.
const limit = await page.evaluate(() => STATUS && STATUS.max_story_chars);
check("limit: the page knows the server's maximum", typeof limit === "number",
      `got ${JSON.stringify(limit)}`);

const counterOk = await page.evaluate(
  () => document.getElementById("storyCount").textContent.trim()
        === `${document.getElementById("story").value.length} / ${STATUS.max_story_chars}`);
check("limit: the counter is measured against it", counterOk,
      await page.textContent("#storyCount"));

// Computed in the page so the thousands separator follows the browser's locale
// rather than this script's.
const placeholderOk = await page.evaluate(
  () => document.getElementById("story").placeholder
          .includes(STATUS.max_story_chars.toLocaleString()));
check("limit: the placeholder quotes it too", placeholderOk,
      await page.getAttribute("#story", "placeholder"));

const overOk = await page.evaluate(() => {
  const el = document.getElementById("story");
  const before = el.value;
  el.value = "x".repeat(STATUS.max_story_chars + 1);
  updateStoryCount();
  const over = document.getElementById("storyCount").classList.contains("over");
  el.value = before;
  updateStoryCount();
  return over;
});
check("limit: one character past it reads as over", overOk);

// ------------------------------------------------------- style direction check
// The Python suite proves the rules and the endpoint. What it cannot prove is
// that the warning reaches the screen, that it clears when the text is fixed,
// and that Segment stops once and then goes ahead -- which is the whole
// behaviour this feature is.
await page.evaluate(() => goStep(1));

const styleBox = page.locator("#styleScreen");
await page.fill("#styleHint", "no pants or jacket, summer outfits");
await page.locator("#styleHint").blur();
await styleBox.locator(".warn").first().waitFor({ state: "visible", timeout: 8000 });

const shown = await styleBox.innerText();
check("style: the refusal risk is shown under the field",
      /no pants/.test(shown), shown.slice(0, 90));
check("style: it says what to write instead",
      /what they are wearing/i.test(shown));
check("style: the matched words are marked",
      (await styleBox.locator(".hit").count()) > 0);

// Segment must stop the first time and go ahead the second.
const gate = await page.evaluate(async () => {
  await screenStyle();
  const before = SCREEN.acknowledged;
  $("segmentBtn").click();
  await new Promise((r) => setTimeout(r, 300));
  return { before, after: SCREEN.acknowledged,
           alert: document.getElementById("alerts").innerText };
});
check("style: the first Segment press stops and explains",
      gate.before === false && gate.after === true && /again/i.test(gate.alert),
      JSON.stringify(gate));

// Editing re-arms it: the acknowledged text is not this text.
await page.fill("#styleHint", "no pants or jacket, summer outfits and a hat");
check("style: editing the text re-arms the check",
      (await page.evaluate(() => SCREEN.acknowledged)) === false);

// And the advice actually clears it.
await page.fill("#styleHint",
                "khaki shorts and canvas sneakers, two men in their twenties");
await page.locator("#styleHint").blur();
await page.waitForFunction(
  () => document.getElementById("styleScreen").innerHTML === "", null,
  { timeout: 8000 });
check("style: following the advice clears the warning", true);

// A clean hint must never have said anything in the first place.
await page.fill("#styleHint", "bold anime linework, cel-shaded, summer 1992");
await page.locator("#styleHint").blur();
await page.waitForTimeout(600);
check("style: ordinary art direction is not flagged",
      (await styleBox.innerHTML()) === "");

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
