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

// The bounds on that box are the server's. `max="40"` used to be typed into the
// markup beside a MAX_SCENE_COUNT nobody published, so tuning the constant left
// the page offering the old range -- and type="number" enforces a max only on a
// form submit, which this page never does, so the limit is said out loud too.
const status = await page.evaluate(() => STATUS);
check("step 1: the image count takes its bounds from the server",
      (await page.getAttribute("#sceneCount", "max")) === String(status.max_scene_count)
      && (await page.getAttribute("#sceneCount", "min")) === "1",
      `max=${await page.getAttribute("#sceneCount", "max")}`);

await page.fill("#sceneCount", String(status.max_scene_count + 1));
await page.evaluate(() => updateSceneCount());
check("step 1: asking for more images than the limit is shown in red",
      await page.locator("#countLimit").evaluate((el) => el.classList.contains("over")),
      await page.locator("#countLimit").innerText());
check("step 1: the limit itself is on screen, not just the red",
      (await page.locator("#countLimit").innerText()).includes(String(status.max_scene_count)));

await page.fill("#sceneCount", "7");
await page.evaluate(() => updateSceneCount());
check("step 1: a count inside the range is not flagged",
      !(await page.locator("#countLimit").evaluate(
        (el) => el.classList.contains("over"))));

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

// ----------------------------------------------------------- picking a subset
// Which scenes a batch is for is settled in tests/test_subset.py against the
// real orchestrator. What only a browser can show is the table doing it: the
// ticks, the count that follows them, and -- the one that costs money if it is
// wrong -- what unticking everything does to the button.
// Step 3 for real, not a call to refreshPlan(): these are clicks on boxes, and
// a click needs the panel to actually be on screen.
await page.evaluate(() => goStep(3));
await page.waitForFunction(
  () => document.querySelectorAll("#planOut [data-pick]").length > 0,
                           null, { timeout: 10000 });

// Scoped to #planOut: the narration plan below has a table of its own, and a
// page-wide locator would count both.
const rows = await page.locator("#planOut [data-pick]").count();
const planned = Number(await page.locator("#cbCount").innerText());
check("pick: every planned scene has a box, and they start ticked",
      rows === planned && rows > 1
      && (await page.locator("#planOut [data-pick]:checked").count()) === rows,
      `${rows} rows, batch of ${planned}`);

// Untick one. The new count comes back from the server, not from subtracting
// one here -- a subset can still drag in a character portrait.
const first = page.locator("#planOut [data-pick]").first();
await first.uncheck();
await page.waitForFunction(
  (n) => document.getElementById("cbCount").textContent.trim() === String(n),
  planned - 1, { timeout: 10000 });
check("pick: unticking a scene narrows the batch", true, `${planned - 1} left`);
check("pick: the page says it is rendering a subset",
      /Rendering \d+ of the \d+/.test(await page.locator("#planOut").innerText()));
// Half-selected is neither on nor off. A header box still reading "all" while
// six of seven are ticked is the control lying about what the button sends.
check("pick: the header box goes indeterminate, not on, for a subset",
      await page.locator("#planOut [data-pick-all]").evaluate(
        (el) => el.indeterminate && !el.checked));

// A click on a half-selected header takes it to all -- the browser's own rule
// for an indeterminate box, and the one people already have in their hands.
await page.locator("#planOut [data-pick-all]").click();
await page.waitForFunction(
  (n) => document.getElementById("cbCount").textContent.trim() === String(n),
  planned, { timeout: 10000 });
check("pick: the header box puts them all back",
      (await page.evaluate(() => PICK_RENDER.numbers())) === null,
      "and a full selection collapses to the request it always sent");

// The one that matters. Unticking everything used to travel as null, and null
// means "the whole batch" -- the opposite of what was asked, and billed.
await page.locator("#planOut [data-pick-all]").uncheck();
await page.waitForFunction(
  () => document.getElementById("cbCount").textContent.trim() === "0",
  null, { timeout: 10000 });
check("pick: unticking everything asks for nothing, not for everything",
      (await page.evaluate(() => PICK_RENDER.numbers().length)) === 0);
check("pick: with nothing selected the spend button is off",
      await page.locator("#renderBtn").isDisabled());
check("pick: and it says what to do about it",
      /Nothing selected/.test(await page.locator("#planOut").innerText()));

await page.locator("#planOut [data-pick-all]").check();
await page.waitForFunction(
  (n) => document.getElementById("cbCount").textContent.trim() === String(n),
  planned, { timeout: 10000 });

await page.evaluate(() => goStep(2));     // back to where the rest of this left off

// The narration plan is the same table with the same boxes, and it is wired
// separately -- one selection object each, so narrowing the render batch must
// not narrow the speaking one.
await page.waitForFunction(
  () => document.querySelectorAll("#ttsPlan [data-pick]").length > 0,
  null, { timeout: 10000 });
await page.locator("#ttsPlan [data-pick]").first().uncheck();
await page.waitForFunction(() => PICK_SPEAK.numbers() !== null, null, { timeout: 10000 });
check("pick: the narration table picks separately from the render one",
      (await page.evaluate(() => PICK_SPEAK.numbers().length))
        === (await page.locator("#ttsPlan [data-pick]").count()) - 1
      && (await page.evaluate(() => PICK_RENDER.numbers())) === null,
      "the render selection is untouched");
await page.locator("#ttsPlan [data-pick-all]").click();
await page.waitForFunction(() => PICK_SPEAK.numbers() === null, null, { timeout: 10000 });

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

// -------------------------------------------------- the prompts, before paying
// Which prompts are risky is settled in tests/test_prompt_risk.py against the
// real compiler. What only a browser can show is the panel above the spend
// button: that it names where the phrase is, quotes the money, and does not
// pretend to be a gate.
const riskState = (risk, count, estimate) => page.evaluate((state) => {
  PLAN = { risk: state.risk, count: state.count, estimate: state.estimate };
  renderRisk();
  return document.getElementById("planRisk").innerText;
}, { risk, count, estimate });

const UNDRESS = {
  code: "undress", severity: "warn", matched: ["no pants"],
  message: "This asks for clothing to be removed or absent ({matched}).",
  suggestion: "Say what they are wearing, not what they are not.",
  scenes: [2, 5], anchors: [],
};

const quietRisk = await riskState({ worst: null, findings: [] }, 6, 0.54);
check("risk: a clean plan shows nothing", quietRisk === "", quietRisk);

const risky = await riskState({ worst: "warn", findings: [UNDRESS] }, 6, 0.54);
check("risk: it quotes the words that triggered it",
      /no pants/.test(risky), risky.split("\n")[1]);
check("risk: it says which scenes they are in",
      /In scenes 2, 5/.test(risky), risky);
check("risk: it says what a refusal costs",
      /\$0\.54/.test(risky) && /billed like any other/.test(risky),
      risky.split("\n").find((l) => /\$/.test(l)));
check("risk: it does not pretend to be a gate",
      /Render anyway/.test(risky) && !(await page.locator("#renderBtn").isDisabled()));
check("risk: a likely refusal is headed as a warning",
      (await page.locator("#planRisk > div").first().getAttribute("class")) === "warn");

// A phrase in the shared block is in every prompt. Listing all six scene
// numbers hides the one fact worth having: it is the block, not a scene.
const everywhere = await riskState(
  { worst: "warn", findings: [{ ...UNDRESS, scenes: [1, 2, 3, 4, 5, 6] }] }, 6, 0.54);
check("risk: a phrase in every prompt is called the style block, not six scenes",
      /shared style block/.test(everywhere) && !/In scenes 1, 2/.test(everywhere),
      everywhere.split("\n").find((l) => /block|scenes/.test(l)));

const portrait = await riskState({ worst: "note", findings: [{
  code: "negation", severity: "note", matched: ["never removes"],
  message: "This tells the engine what not to draw ({matched}).",
  suggestion: "Rewrite it as something to include.",
  scenes: [], anchors: ["mona"] }] }, 6, 0.54);
check("risk: a character portrait is named by who it is of",
      /reference portrait for mona/.test(portrait), portrait);
check("risk: a quality note is not dressed up as a refusal",
      (await page.locator("#planRisk > div").first().getAttribute("class")) === "note"
      && (await page.locator("#planRisk .warn").count()) === 0);
// It risks no money and there is nothing to overrule, so it is not given the
// price of the batch or an invitation to go ahead anyway.
check("risk: a note is not priced or argued with",
      !/billed like any other/.test(portrait) && !/Render anyway/.test(portrait),
      portrait);

await page.evaluate(() => { document.getElementById("planRisk").innerHTML = ""; });

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

// ------------------------------------------------------------- the 529 wait
// The plumbing -- the ladder, the countdown, the fallback -- is proven in
// tests/test_overload.py against the real compiler. What only a browser can
// show is the panel it all ends up in, so these drive the renderer with the
// states the server actually sends.
const waitBox = page.locator("#segmentWait");

const waiting = await page.evaluate(() => {
  drawWait("segmentWait", { running: true, phase: "waiting", attempt: 2, of: 5,
                            reason: "overloaded", seconds: 8, retry_in: 6,
                            model: "claude-opus-5", falling_back: false });
  return document.getElementById("segmentWait").innerText;
});
check("wait: a 529 says who is busy and that nothing was charged",
      /capacity/i.test(waiting) && /never charged/i.test(waiting),
      waiting.split("\n")[1]);
check("wait: it counts down and numbers the attempt",
      /6s/.test(waiting) && /attempt 2 of 5/.test(waiting));
check("wait: it says how long it will keep trying",
      /51s/.test(waiting), waiting);
check("wait: the bar shows how much of this pause has run",
      (await waitBox.locator(".bar i").getAttribute("style")) === "width:25%",
      await waitBox.locator(".bar i").getAttribute("style"));

const fell = await page.evaluate(() => {
  drawWait("segmentWait", { running: true, phase: "waiting", attempt: 5, of: 5,
                            reason: "overloaded", seconds: 20, retry_in: 20,
                            model: "claude-sonnet-5", falling_back: true });
  return document.getElementById("segmentWait").innerText;
});
check("wait: a fallback is named before the other model answers",
      /claude-opus-5/.test(fell) && /claude-sonnet-5/.test(fell),
      fell.split("\n").find((l) => /instead/.test(l)) || fell);

const limited = await page.evaluate(() => {
  drawWait("segmentWait", { running: true, phase: "waiting", attempt: 2, of: 5,
                            reason: "rate_limit", seconds: 3, retry_in: 3 });
  return document.getElementById("segmentWait").innerText;
});
check("wait: a rate limit is not described as an outage",
      /rate limited/i.test(limited) && !/capacity/i.test(limited),
      limited.split("\n")[0]);

// Attempt one going normally is what the button already says; a second line
// repeating it is noise.
const quiet = await page.evaluate(() => {
  drawWait("segmentWait", { running: true, phase: "calling", attempt: 1, of: 5 });
  return document.getElementById("segmentWait").innerHTML;
});
check("wait: a first attempt going fine says nothing", quiet === "", quiet);

const retryQuiet = await page.evaluate(() => {
  drawWait("segmentWait", { running: true, phase: "calling", attempt: 3, of: 5 });
  return document.getElementById("segmentWait").innerText;
});
check("wait: a later attempt still says which one it is on",
      /attempt 3 of 5/.test(retryQuiet), retryQuiet);

const done = await page.evaluate(() => {
  drawWait("segmentWait", { running: false });
  return document.getElementById("segmentWait").innerHTML;
});
check("wait: nothing lingers once the call is over", done === "", done);

// --------------------------------------------------------------- out of date
// Which work is stale is settled in tests/test_stale.py against the real
// project. What only a browser can show is whether the warning is on screen at
// the two places work leaves the project -- the cut and the export -- and
// whether it stays out of the way when there is nothing to say.
const staleState = (st) => page.evaluate((stale) => {
  P.stale = stale;
  renderStale("videoStale", "stale.cut.h", true);
  renderStale("exportStale", "stale.export.h", false);
  return { cut: document.getElementById("videoStale").innerText,
           exp: document.getElementById("exportStale").innerText };
}, st);

const clean = await staleState(
  { images: [], audio: [], missing_images: [], unspoken: [] });
check("stale: a project with nothing out of date shows nothing",
      clean.cut === "" && clean.exp === "", clean.cut + clean.exp);

const messy = await staleState(
  { images: [2, 5], audio: [3], missing_images: [7], unspoken: [4] });
check("stale: the export warns before the button, not after the folder",
      /2 of \d+/.test(messy.exp) && /scenes 2, 5/.test(messy.exp),
      messy.exp.split("\n")[1]);
check("stale: an edited line is called out separately from a stale picture",
      /edited after they were spoken/.test(messy.exp)
      && /scenes 3/.test(messy.exp));
check("stale: a scene with no image is named rather than silently dropped",
      /Left out of the folder entirely/.test(messy.exp) && /scenes 7/.test(messy.exp));
check("stale: it says plainly that nothing is blocked",
      /Nothing here is blocked/.test(messy.exp), messy.exp.split("\n").pop());

// An unspoken line is not out of date -- it is an estimate, and only the cut's
// runtime depends on it. The export copies the text either way.
check("stale: only the cut mentions lines that have no voice yet",
      /word-count estimate/.test(messy.cut) && !/word-count estimate/.test(messy.exp));

// Warnings are amber, not decoration: it has to be a .warn box, or it reads as
// another hint and gets skipped like every other hint on the page. Asserted
// while something is still wrong -- the box is empty once nothing is.
check("stale: the warning is a warning",
      (await page.locator("#exportStale .warn").count()) === 1);

// One scene, not two: "1 lines were edited" is how a warning starts reading as
// machine output, and the counts here come from the server every time.
const one = await staleState(
  { images: [4], audio: [], missing_images: [], unspoken: [] });
check("stale: a single scene reads as English, not as a count",
      !/1 lines|1 scenes/.test(one.exp) && /scenes 4/.test(one.exp),
      one.exp.split("\n")[1]);

const onlyUnspoken = await staleState(
  { images: [], audio: [], missing_images: [], unspoken: [4] });
check("stale: an export with only unspoken lines stays quiet",
      onlyUnspoken.exp === "" && /word-count estimate/.test(onlyUnspoken.cut),
      onlyUnspoken.exp);

await page.evaluate(() => {
  P.stale = { images: [], audio: [], missing_images: [], unspoken: [] };
  renderStale("videoStale", "stale.cut.h", true);
  renderStale("exportStale", "stale.export.h", false);
});

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
