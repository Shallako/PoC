#!/usr/bin/env python3
"""Start the local Shoulico server.

    python run.py                # http://127.0.0.1:8765
    python run.py --port 9000
    python run.py --no-browser

Binds to loopback only. Nothing here is hardened for a public interface.
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser

from shoulico import config


def main() -> int:
    ap = argparse.ArgumentParser(description="Shoulico local MVP")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--reload", action="store_true", help="dev auto-reload")
    args = ap.parse_args()

    try:
        import uvicorn
    except ImportError:
        print("uvicorn is not installed. Run:  pip install -r requirements.txt")
        return 1

    config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

    keys = config.key_status()
    url = f"http://{args.host}:{args.port}"
    print(f"Shoulico  ->  {url}")
    print(f"  projects : {config.PROJECTS_DIR}")
    print(f"  Renderful key : {'found' if keys['renderful'] else 'MISSING (image render disabled)'}")
    print(f"  Anthropic key : {'found' if keys['anthropic'] else 'MISSING (segmentation/narration disabled)'}")
    print("  Ctrl-C to stop.\n")

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run("shoulico.app:app", host=args.host, port=args.port,
                reload=args.reload, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
