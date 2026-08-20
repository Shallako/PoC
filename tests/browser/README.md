# Browser check

The Python suite drives the real FastAPI app and proves the API. It cannot prove
that a textarea keeps its text, its focus and its caret while the page redraws
underneath it — and the render poller redraws step 2 every 2.5 seconds. That is
what this checks, in a real Chromium.

It exists because three UI fixes shipped unverified: there was no JavaScript
runtime on the machine at the time, so they were reasoned about and served, but
never driven.

## Once

    winget install OpenJS.NodeJS.LTS        # or any Node 20+
    cd tests/browser
    npm install
    npx playwright install chromium

## Every time

Seed a project with scenes (so nothing calls a paid API), start the app, then:

    node check.mjs http://127.0.0.1:8810 <project-id>

Exit status is 0 when every check passes. It asserts:

* the page opens on a blank new project rather than the last one worked on,
  with the existing projects one click away, nothing to delete, and no project
  written by opening or reloading;
* choosing *New project* clears the form and still writes nothing, and picking a
  setting is what finally writes it -- under the name typed into the form;
* step 1 keeps the story and the image count across a settings redraw;
* the image count takes both bounds from the server and says the limit out loud,
  in red once it is passed;
* step 2 keeps a scene edit, the focus and the caret across a poll redraw;
* the style block survives the same;
* the plan table tick-boxes narrow the batch, the count follows them from the
  server, the header box goes indeterminate for a subset, unticking everything
  asks for nothing rather than everything and turns the spend button off, the
  header box puts them all back, and the narration table picks separately from
  the render one;
* the style direction shows its refusal warning, names what to write instead,
  clears when the advice is followed, stops Segment once and then lets it
  through, and re-arms when the text is edited;
* the pre-spend prompt reading names the words, the scenes and the portraits it
  found them in, calls a phrase that is in every prompt the style block rather
  than listing every scene, quotes what a refusal would cost, tells a warning
  from a note, and leaves the spend button enabled;
* the 529 wait panel counts down, numbers the attempt, quotes the server's own
  patience, names a fallback, tells a rate limit from an outage, and stays quiet
  on a first attempt that is going fine;
* the out-of-date warning appears above both the assemble and the export button,
  names the scenes, separates a stale picture from an edited line from a missing
  image, says nothing is blocked, keeps unspoken lines to the cut, and disappears
  entirely when there is nothing to report;
* both Claude-phase cancel buttons exist and are hidden while idle;
* the page script carries its CSP nonce;
* nothing lands in the console — a CSP violation shows up here and nowhere else.

## Why it is not in `pytest`

It needs a Node toolchain and a 150MB browser download, which the rest of the
suite does not. Keeping it separate means `pytest` stays a single free command
on a clean checkout, the same way the `--live` tests are opt-in for costing
money rather than for being slow.
