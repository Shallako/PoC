"""A stand-in for the ffmpeg binary.

The seam is `subprocess.run`, not `video._run`, deliberately: every argument
this project builds -- the filtergraphs, the codec flags, the concat list, the
ordering of inputs -- is constructed for real and recorded, so a test can assert
on the actual command line. What it cannot prove is that ffmpeg *accepts* that
command line. Nothing offline can, so that claim is left to the live test, which
skips unless a real ffmpeg is installed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

FAKE_EXE = "ffmpeg-under-test"
FAKE_VERSION = "ffmpeg version 7.1-fake Copyright (c) 2000-2026 the FFmpeg developers"

# Enough bytes that a written file is obviously a file and not an empty stub.
_PAYLOAD = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


class FakeFfmpeg:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.cwds: list[str | None] = []
        self.fail_on: str | None = None      # substring: any call containing it fails

    # -- what the tests read ------------------------------------------------ #

    @property
    def encodes(self) -> list[list[str]]:
        """Calls that built a scene segment."""
        return [c for c in self.calls if "-filter_complex" in c]

    @property
    def joins(self) -> list[list[str]]:
        return [c for c in self.calls if "concat" in c]

    def filtergraph(self, i: int = 0) -> str:
        call = self.encodes[i]
        return call[call.index("-filter_complex") + 1]

    def flag(self, call: list[str], name: str) -> str | None:
        return call[call.index(name) + 1] if name in call else None

    # -- the stand-in itself ------------------------------------------------ #

    def run(self, args, **kwargs):
        args = [str(a) for a in args]
        self.calls.append(args)
        cwd = kwargs.get("cwd")
        self.cwds.append(cwd)

        if "-version" in args:
            return subprocess.CompletedProcess(args, 0, stdout=FAKE_VERSION + "\n",
                                               stderr="")
        if self.fail_on and any(self.fail_on in a for a in args):
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr="fake ffmpeg was told to fail here")

        # ffmpeg's output file is its last argument, relative to cwd when set.
        dest = Path(args[-1])
        if not dest.is_absolute() and cwd:
            dest = Path(cwd) / dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_PAYLOAD)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def install(monkeypatch, video_module) -> FakeFfmpeg:
    """Make the app believe ffmpeg is installed, and capture what it asks of it."""
    fake = FakeFfmpeg()
    monkeypatch.setattr(video_module, "ffmpeg_path", lambda: FAKE_EXE)
    monkeypatch.setattr(video_module.subprocess, "run", fake.run)
    return fake
