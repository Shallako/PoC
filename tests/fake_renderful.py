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

# A real MPEG1 Layer III stream, so shoulico.audio parses it for real rather than
# against a stub. Header FF FB 90 00: sync, MPEG1, Layer III, no CRC, 128 kbit/s,
# 44100 Hz, no padding, stereo.
AUDIO_TYPE = "text-to-audio"

MP3_FRAME_HEADER = b"\xff\xfb\x90\x00"
MP3_BITRATE = 128_000
MP3_SAMPLE_RATE = 44_100
MP3_SAMPLES_PER_FRAME = 1152
MP3_FRAME_BYTES = 144 * MP3_BITRATE // MP3_SAMPLE_RATE          # 417
MP3_FRAME_SECONDS = MP3_SAMPLES_PER_FRAME / MP3_SAMPLE_RATE     # ~0.026


def silent_mp3(frames: int = 38) -> bytes:
    """A decodable silent MP3. 38 frames is a shade under one second."""
    body = MP3_FRAME_HEADER + b"\x00" * (MP3_FRAME_BYTES - len(MP3_FRAME_HEADER))
    return body * frames


def mp3_seconds(frames: int = 38) -> float:
    """What shoulico.audio should measure for `silent_mp3(frames)`.

    No Xing header is written, so the CBR path applies: bytes over bitrate.
    """
    return frames * MP3_FRAME_BYTES * 8 / MP3_BITRATE


SILENT_MP3 = silent_mp3()


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
        # Sent as Retry-After on any 4xx/5xx, so the client's honouring of the
        # server's own "how long?" can be tested rather than assumed.
        self.retry_after: str | None = None
        self.polls_before_complete = 1
        self.fail_generation: set[str] = set()
        # Fail whichever generation carries this text in its prompt. `gen-N` ids
        # are handed out in arrival order, and workers run concurrently, so
        # targeting a specific generation by id is only deterministic when the
        # thing under test is single-threaded. Content is stable either way.
        self.fail_prompt_containing: str | None = None
        self.fail_all_generations = False
        self.no_outputs = False
        self.download_status = 200
        self.payload = ONE_PIXEL_JPEG
        self.cost = 0.04
        # Speech jobs are the same endpoint with a different `type`, so they are
        # served here rather than by a second fake.
        self.audio_payload = SILENT_MP3
        self.audio_cost = 0.0035

        # Observed concurrency: how many generations were open at once.
        self.in_flight = 0
        self.peak_in_flight = 0
        # ("submit" | "done", prompt), in order. Peak concurrency cannot answer
        # "did this start before that finished"; the ordering can. The fake stays
        # ignorant of what a prompt means -- the test classifies it.
        self.timeline: list[tuple[str, str]] = []

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
        # poll_interval, not the 0.5s default: shutdown() blocks for up to one
        # interval, and this server is started and stopped once per test. Half a
        # second of doing nothing, a few hundred times, was most of the suite's
        # runtime -- the tests themselves are fast.
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01},
                         daemon=True, name="fake-renderful").start()
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

    def submits_of_type(self, gen_type: str) -> list[dict]:
        with self.lock:
            return [s["payload"] for s in self.submits
                    if s["payload"].get("type") == gen_type]

    def references_sent(self) -> list[list[str]]:
        """The `images` array of every submission that carried one, in order."""
        with self.lock:
            return [list(s["payload"]["images"]) for s in self.submits
                    if s["payload"].get("images")]

    def payload_for_prompt(self, needle: str) -> dict | None:
        """The first submission whose prompt contains `needle`."""
        with self.lock:
            for s in self.submits:
                if needle in s["payload"].get("prompt", ""):
                    return s["payload"]
            return None

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
            self.timeline.append(("done", self._generations[gid].get("prompt", "")))

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
            gen_type = payload.get("type", "")
            self._generations[gid] = {"polls": 0, "prompt": payload.get("prompt", ""),
                                      "type": gen_type}
            self._open(gid)
            self.timeline.append(("submit", payload.get("prompt", "")))
            cost = self.audio_cost if gen_type == AUDIO_TYPE else self.cost
            return 200, {"id": gid, "status": "queued", "cost": cost}

    def handle_poll(self, gid: str) -> tuple[int, dict]:
        with self.lock:
            self.polls.append(gid)
            gen = self._generations.get(gid)
            if gen is None:
                return 404, {"message": f"no generation {gid}"}
            gen["polls"] += 1

            targeted = (self.fail_prompt_containing is not None
                        and self.fail_prompt_containing in gen.get("prompt", ""))
            if self.fail_all_generations or gid in self.fail_generation or targeted:
                self._close(gid)
                return 200, {"id": gid, "status": "failed",
                             "error": "the engine rejected this prompt"}

            if gen["polls"] < self.polls_before_complete:
                return 200, {"id": gid, "status": "processing"}

            self._close(gid)
            is_audio = gen.get("type") == AUDIO_TYPE
            doc = {"id": gid, "status": "completed",
                   "cost": self.audio_cost if is_audio else self.cost}
            if not self.no_outputs:
                doc["outputs"] = [f"{self.root}/files/{gid}.{'mp3' if is_audio else 'jpg'}"]
            return 200, doc

    def handle_download(self, name: str) -> tuple[int, bytes]:
        with self.lock:
            self.downloads.append(name)
            if self.download_status != 200:
                return self.download_status, b"gone"
            if name.endswith(".mp3"):
                return 200, self.audio_payload
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
            body = json.dumps(doc).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if code >= 400 and fake.retry_after is not None:
                self.send_header("Retry-After", str(fake.retry_after))
            self.end_headers()
            self.wfile.write(body)

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
                    self._send(200, body,
                               "audio/mpeg" if name.endswith(".mp3") else "image/jpeg")
                return
            self._send_json(404, {"message": f"no route {self.path}"})

    return Handler
