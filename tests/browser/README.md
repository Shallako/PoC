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

* step 1 keeps the story and the image count across a settings redraw;
* step 2 keeps a scene edit, the focus and the caret across a poll redraw;
* the style block survives the same;
* both Claude-phase cancel buttons exist and are hidden while idle;
* the page script carries its CSP nonce;
* nothing lands in the console — a CSP violation shows up here and nowhere else.

## Why it is not in `pytest`

It needs a Node toolchain and a 150MB browser download, which the rest of the
suite does not. Keeping it separate means `pytest` stays a single free command
on a clean checkout, the same way the `--live` tests are opt-in for costing
money rather than for being slow.
