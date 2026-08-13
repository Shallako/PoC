"""SRT and WebVTT built from the measured timeline.

A caption is not a narration line. One scene's line can run fifteen seconds and
two hundred characters; holding all of that on screen at once is unreadable, so
a line is split into cues at sentence boundaries and the scene's *measured*
speech time is shared out between them in proportion to their length.

That proportional split is an approximation -- it assumes an even speaking rate
within a line, which is not exactly true. It is honest at the scene boundary,
which is what matters: every cue for a scene lies inside that scene's real audio,
so drift can never accumulate across the video the way it would if each cue were
timed from a word-count guess. Forced alignment would tighten the inside of a
line, and is the obvious next step if it ever reads as loose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import config, narration
from .timeline import Beat

# Sentence enders for spaced scripts, and for scripts that use their own marks.
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|(?<=[。！？；])\s*")
# Clause breaks, used only when a single sentence is still too long for one cue.
_CLAUSE_END = re.compile(r"(?<=[,;:])\s+|(?<=[，、；：])\s*")

MAX_CHARS_PER_CUE = config.CAPTION_MAX_CHARS_PER_LINE * config.CAPTION_MAX_LINES


@dataclass(frozen=True)
class Cue:
    index: int
    start: float
    end: float
    text: str          # already wrapped, newline-separated
    scene: int

    @property
    def seconds(self) -> float:
        return round(self.end - self.start, 3)


def _split(text: str, limit: int) -> list[str]:
    """Break a line into pieces that each fit a cue, preferring natural breaks."""
    pieces = [p.strip() for p in _SENTENCE_END.split(text) if p and p.strip()]
    out: list[str] = []
    for piece in pieces:
        if len(piece) <= limit:
            out.append(piece)
            continue
        # Too long even as one sentence: try clauses, then fall back to hard wrap.
        for clause in (c.strip() for c in _CLAUSE_END.split(piece) if c and c.strip()):
            out.extend([clause] if len(clause) <= limit else _hard_wrap(clause, limit))
    return out or ([text.strip()] if text.strip() else [])


def _hard_wrap(text: str, limit: int) -> list[str]:
    """Last resort. Words where the script has them, characters where it doesn't."""
    if narration.unspaced(text):
        return [text[i:i + limit] for i in range(0, len(text), limit)]

    lines, current = [], ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > limit:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _pack(pieces: list[str]) -> list[str]:
    """Join neighbouring pieces that comfortably share a cue.

    Two short sentences in one cue read better than two cues that each flash for
    under a second.
    """
    packed: list[str] = []
    for piece in pieces:
        if packed and len(packed[-1]) + 1 + len(piece) <= MAX_CHARS_PER_CUE:
            packed[-1] = f"{packed[-1]} {piece}".strip()
        else:
            packed.append(piece)
    return packed


def wrap(text: str) -> str:
    """Lay a cue out over at most the allowed number of lines."""
    lines = _hard_wrap(text, config.CAPTION_MAX_CHARS_PER_LINE)
    if len(lines) <= config.CAPTION_MAX_LINES:
        return "\n".join(lines)
    # More lines than allowed: rebalance onto the maximum, wider if it must be.
    per = max(config.CAPTION_MAX_CHARS_PER_LINE,
              -(-len(text) // config.CAPTION_MAX_LINES))
    return "\n".join(_hard_wrap(text, per)[:config.CAPTION_MAX_LINES] or [text])


def cues_for(beat: Beat, start_index: int = 1) -> list[Cue]:
    """One scene's cues, timed inside that scene's own speech window."""
    if not beat.text or beat.speech_seconds <= 0:
        return []

    pieces = _pack(_split(beat.text, MAX_CHARS_PER_CUE))
    if not pieces:
        return []

    total_chars = sum(len(p) for p in pieces) or 1
    out: list[Cue] = []
    clock = beat.speech_start
    for i, piece in enumerate(pieces):
        if clock >= beat.speech_end:
            break                      # the line outran its own audio
        # The last cue takes whatever is left rather than its own share, so
        # rounding cannot leave the scene's captions short of its audio.
        if i == len(pieces) - 1:
            end = beat.speech_end
        else:
            share = beat.speech_seconds * len(piece) / total_chars
            end = clock + max(share, config.CAPTION_MIN_SECONDS)
        # Never past the scene's own speech, and never longer than a reader needs.
        end = min(end, clock + config.CAPTION_MAX_SECONDS, beat.speech_end)
        if end <= clock:
            break

        out.append(Cue(
            index=start_index + len(out),
            start=round(clock, 3),
            end=round(end, 3),
            text=wrap(piece),
            scene=beat.n,
        ))
        clock = end + config.CAPTION_GAP_SECONDS

    return out


def build(beats: list[Beat]) -> list[Cue]:
    cues: list[Cue] = []
    for beat in beats:
        cues.extend(cues_for(beat, start_index=len(cues) + 1))
    return cues


def _stamp(seconds: float, millis_sep: str) -> str:
    if seconds < 0:
        seconds = 0.0
    whole = int(seconds)
    ms = int(round((seconds - whole) * 1000))
    if ms == 1000:            # 3.9999 must not print as 00:00:03,1000
        whole, ms = whole + 1, 0
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}{millis_sep}{ms:03d}"


def to_srt(cues: list[Cue]) -> str:
    blocks = [f"{c.index}\n{_stamp(c.start, ',')} --> {_stamp(c.end, ',')}\n{c.text}"
              for c in cues]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def to_vtt(cues: list[Cue]) -> str:
    blocks = [f"{_stamp(c.start, '.')} --> {_stamp(c.end, '.')}\n{c.text}" for c in cues]
    return "WEBVTT\n\n" + "\n\n".join(blocks) + ("\n" if blocks else "")
