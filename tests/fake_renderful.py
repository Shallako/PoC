"""A stand-in for api.renderful.ai, served on loopback.

The point of testing against a real socket rather than a monkeypatched function
is that everything in renderful.py -- the Bearer header, the retry ladder, the
poll loop, the 4xx split, saving the bytes as delivered -- runs for real. Only
the far end is fake, so no request ever leaves the machine and nothing is billed.

Every failure mode the client claims to handle is scriptable here:

    fake.submit_error = (402, {"message": "credit limit reached"})   # fatal
    fake.submit_flaky = [500, 500]                                   # then succeed
    fake.fail_generation.add("gen-2")                                # engine failure
    fake.no_outputs = True                                           # completed, empty
    fake.download_status = 404                                       # dead asset URL
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# A real 1x1 JPEG. Renderful delivers JPEG whatever output_format you ask for,
# and the suite asserts the saved file follows the bytes, not the request.
ONE_PIXEL_JPEG = base64.b64decode(
    b"/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    b"Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB"
    b"AAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
)

ONE_PIXEL_PNG = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
    b"IQAAAABJRU5ErkJggg=="
)


class FakeRenderful:
    def __init__(self) -> None:
        self.lock = threading.RLock()

        # Everything the client sent, for assertions.
        self.submits: list[dict] = []      # [{"payload": {...}, "auth": "Bearer ..."}]
        self.polls: list[str] = []         # generation ids, in order
        self.downloads: list[str] = []     # file names

        # Knobs.
        self.submit_error: tuple[int, dict] | None = None
        self.submit_flaky: list[int] = []
        self.polls_before_complete = 1
        self.fail_generation: set[str] = set()
        self.fail_all_generations = False
        self.no_outputs = False
        self.download_status = 200
        self.payload = ONE_PIXEL_JPEG
        self.cost = 0.04

        # Observed concurrency: how many generations were open at once.
        self.in_flight = 0
        self.peak_in_flight = 0

        self._seq = 0
        self._generations: dict[str, dict] = {}
        self._closed: set[str] = set()
        self._server: ThreadingHTTPServer | None = None
        self.root = ""
        self.base = ""

    # ----------------------------------------------------------------- #
    # Lifecycle
    # ----------------------------------------------------------------- #

    def start(self) -> "FakeRenderful":
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(self))
        self.root = f"http://127.0.0.1:{self._server.server_address[1]}"
        self.base = self.root + "/api/v1"
        threading.Thread(target=self._server.serve_forever, daemon=True,
                         name="fake-renderful").start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # ----------------------------------------------------------------- #
    # Introspection helpers
    # ----------------------------------------------------------------- #

    @property
    def submit_count(self) -> int:
        with self.lock:
            return len(self.submits)

    def prompts(self) -> list[str]:
        with self.lock:
            return [s["payload"].get("prompt", "") for s in self.submits]

    def reset_counters(self) -> None:
        with self.lock:
            self.submits.clear()
            self.polls.clear()
            self.downloads.clear()

    # ----------------------------------------------------------------- #
    # Routes
    # ----------------------------------------------------------------- #

    def _open(self, gid: str) -> None:
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)

    def _close(self, gid: str) -> None:
        if gid not in self._closed:
            self._closed.add(gid)
            self.in_flight -= 1

    def handle_submit(self, payload: dict, auth: str) -> tuple[int, dict]:
        with self.lock:
            self.submits.append({"payload": payload, "auth": auth})
            if self.submit_error is not None:
                return self.submit_error
            if self.submit_flaky:
                code = self.submit_flaky.pop(0)
                return code, {"message": f"synthetic {code}"}
            self._seq += 1
            gid = f"gen-{self._seq}"
            self._generations[gid] = {"polls": 0, "prompt": payload.get("prompt", "")}
            self._open(gid)
            return 200, {"id": gid, "status": "queued", "cost": self.cost}

    def handle_poll(self, gid: str) -> tuple[int, dict]:
        with self.lock:
            self.polls.append(gid)
            gen = self._generations.get(gid)
            if gen is None:
                return 404, {"message": f"no generation {gid}"}
            gen["polls"] += 1

            if self.fail_all_generations or gid in self.fail_generation:
                self._close(gid)
                return 200, {"id": gid, "status": "failed",
                             "error": "the engine rejected this prompt"}

            if gen["polls"] < self.polls_before_complete:
                return 200, {"id": gid, "status": "processing"}

            self._close(gid)
            doc = {"id": gid, "status": "completed", "cost": self.cost}
            if not self.no_outputs:
                doc["outputs"] = [f"{self.root}/files/{gid}.jpg"]
            return 200, doc

    def handle_download(self, name: str) -> tuple[int, bytes]:
        with self.lock:
            self.downloads.append(name)
            if self.download_status != 200:
                return self.download_status, b"gone"
            return 200, self.payload


def _handler_for(fake: FakeRenderful):
    class Handler(BaseHTTPRequestHandler):
        server_version = "FakeRenderful/1.0"

        def log_message(self, *args) -> None:  # keep pytest output clean
            pass

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, code: int, doc: dict) -> None:
            self._send(code, json.dumps(doc).encode("utf-8"), "application/json")

        def do_POST(self) -> None:
            if self.path != "/api/v1/generations":
                self._send_json(404, {"message": f"no route {self.path}"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except ValueError:
                self._send_json(400, {"message": "malformed json"})
                return
            code, doc = fake.handle_submit(payload, self.headers.get("Authorization", ""))
            self._send_json(code, doc)

        def do_GET(self) -> None:
            if self.path.startswith("/api/v1/generations/"):
                gid = self.path.rsplit("/", 1)[-1]
                code, doc = fake.handle_poll(gid)
                self._send_json(code, doc)
                return
            if self.path.startswith("/files/"):
                name = self.path.rsplit("/", 1)[-1]
                code, body = fake.handle_download(name)
                if code != 200:
                    self._send_json(code, {"message": "gone"})
                else:
                    self._send(200, body, "image/jpeg")
                return
            self._send_json(404, {"message": f"no route {self.path}"})

    return Handler
