#!/usr/bin/env python3
"""Start the local Shoulico server.

    python run.py                # http://127.0.0.1:8765
    python run.py --port 9000
    python run.py --no-browser

Binds to loopback. The API has no login and spends real money on your Renderful
key, so binding it anywhere reachable takes --allow-remote and means it.
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser

from shoulico import config

LOOPBACK = {"127.0.0.1", "::1", "localhost"}

# "Every interface" is not an address a browser can open. Map each to the
# loopback address in the same family for display and for the readiness probe.
ANY_ADDRESS = {"0.0.0.0": "127.0.0.1", "": "127.0.0.1", "::": "::1", "::0": "::1"}


def browsable(host: str, port: int) -> str:
    """A URL that actually opens.

    Two ways the old one-liner produced a dead link: `http://0.0.0.0:8765` sends
    the browser to an address that cannot be connected to, and an IPv6 literal
    without brackets (`http://::1:8765`) is not a URL at all.
    """
    host = ANY_ADDRESS.get(host, host)
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}"


def port_is_taken(host: str, port: int) -> bool:
    """Bind it ourselves first, so a busy port is a sentence and not a traceback.

    Inherently racy -- something could claim the port in the gap -- but the case
    this catches is the last Shoulico still running, and if the race is lost
    uvicorn still reports the collision itself.
    """
    try:
        infos = socket.getaddrinfo(host or None, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False              # unresolvable: let uvicorn give the real error
    family, socktype, proto = infos[0][:3]
    addr = infos[0][4]
    with socket.socket(family, socktype, proto) as probe:
        try:
            probe.bind(addr)
        except OSError:
            return True
    return False


def open_when_ready(url: str, host: str, port: int, timeout: float = 20.0) -> None:
    """Open the browser once the port answers, rather than one second from now.

    A cold start imports fastapi, uvicorn, the app and its registry; on a slow
    disk that outlasts any fixed delay, and the browser lands on a connection
    error the user then has to reload by hand.
    """
    target = ANY_ADDRESS.get(host, host)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((target, port), timeout=0.5):
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(0.2)
    # Never came up. A tab pointing at nothing would only add to the confusion.


def main() -> int:
    ap = argparse.ArgumentParser(description="Shoulico local MVP")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--reload", action="store_true", help="dev auto-reload")
    ap.add_argument("--allow-remote", action="store_true",
                    help="permit a non-loopback --host. There is no login: "
                         "anyone who can reach it can spend your Renderful credit")
    args = ap.parse_args()

    if not 1 <= args.port <= 65535:
        print(f"--port must be between 1 and 65535, not {args.port}.")
        return 2

    # The docstring promised loopback while --host quietly accepted 0.0.0.0.
    # This server has no authentication and every render button spends money, so
    # exposing it has to be a decision, not a typo.
    if args.host not in LOOPBACK and not args.allow_remote:
        print(f"Refusing to bind to {args.host}: Shoulico has no login, and anyone who "
              f"can reach it can spend your Renderful credit.\n"
              f"Keep the default 127.0.0.1, or pass --allow-remote if you meant it.")
        return 2

    try:
        import uvicorn
    except ImportError:
        print("uvicorn is not installed. Run:  pip install -r requirements.txt")
        return 1

    if port_is_taken(args.host, args.port):
        print(f"Port {args.port} is already in use -- most likely a Shoulico that is "
              f"still running.\nStop that one, or start this one elsewhere:  "
              f"python run.py --port {args.port + 1}")
        return 1

    config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

    keys = config.key_status()
    url = browsable(args.host, args.port)
    print(f"Shoulico  ->  {url}")
    print(f"  projects : {config.PROJECTS_DIR}")
    print(f"  Renderful key : "
          f"{'found' if keys['renderful'] else 'MISSING (rendering and narration audio disabled)'}")
    print(f"  Anthropic key : "
          f"{'found' if keys['anthropic'] else 'MISSING (segmentation/narration disabled)'}")
    # A key that is present but malformed shows green in the UI's found/missing
    # boolean and then 401s at the moment it is used. The UI says so; so does this.
    if keys["anthropic_warning"]:
        print(f"  ! {keys['anthropic_warning']}")
    if args.host not in LOOPBACK:
        print(f"  ! Reachable from the network on {args.host}. No login, real money.")
    print("  Ctrl-C to stop.\n")
    # uvicorn logs to stderr, which is unbuffered; this banner goes to stdout,
    # which is block-buffered the moment output is redirected to a file. Without
    # this flush the line carrying the URL arrives *after* uvicorn's startup
    # noise, or not until the process exits.
    sys.stdout.flush()

    if not args.no_browser:
        # Daemon: Ctrl-C during startup should exit now, not block until a
        # browser nobody is waiting for has been launched.
        threading.Thread(target=open_when_ready, args=(url, args.host, args.port),
                         daemon=True).start()

    uvicorn.run("shoulico.app:app", host=args.host, port=args.port,
                reload=args.reload, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
