"""Measured audio duration, in pure Python.

The whole point of synthesising narration is to replace the word-count estimate
with a fact, so this has to be reliable. ffprobe is not installed on the target
machine and the project deliberately carries no hard third-party dependency
(stdlib urllib, not requests; Pillow optional-only), so the container headers are
parsed here instead.

Nothing in here raises. A duration that cannot be read returns None and the
caller falls back to `narration.estimate_seconds()` -- degrading to the old
behaviour is always better than failing a render that has already been paid for.
"""

from __future__ import annotations

import struct
from pathlib import Path

# --------------------------------------------------------------------------- #
# MPEG audio frame header tables (ISO/IEC 11172-3, 13818-3)
# --------------------------------------------------------------------------- #

_ID3_MAGIC = b"ID3"
_ID3_HEADER_BYTES = 10
_ID3_FOOTER_FLAG = 0x10
_SYNCSAFE_BITS = 7                      # ID3 sizes use 7 usable bits per byte
_SYNCSAFE_MASK = 0x7F

_FRAME_HEADER_BYTES = 4
_FRAME_SYNC_MASK = 0xFFE0               # 11 set bits: sync word
_FRAME_SYNC_VALUE = 0xFFE0

_MPEG_V2_5, _MPEG_RESERVED, _MPEG_V2, _MPEG_V1 = 0, 1, 2, 3
_LAYER_RESERVED, _LAYER_III, _LAYER_II, _LAYER_I = 0, 1, 2, 3

_BITRATE_RESERVED_INDEX = 0
_BITRATE_BAD_INDEX = 0xF
_SAMPLE_RATE_RESERVED_INDEX = 3
_BITS_PER_KILOBIT = 1000                # bitrate tables are in kbit/s

# kbit/s by bitrate index. Index 0 (free) and 15 (bad) are rejected before use.
_BITRATES_V1 = {
    _LAYER_I: (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448, 0),
    _LAYER_II: (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 0),
    _LAYER_III: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0),
}
_BITRATES_V2 = {
    _LAYER_I: (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, 0),
    _LAYER_II: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
    _LAYER_III: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
}

_SAMPLE_RATES = {
    _MPEG_V1: (44100, 48000, 32000),
    _MPEG_V2: (22050, 24000, 16000),
    _MPEG_V2_5: (11025, 12000, 8000),
}

# Samples emitted per frame. Layer III halves on MPEG2/2.5.
_SAMPLES_PER_FRAME = {
    _MPEG_V1: {_LAYER_I: 384, _LAYER_II: 1152, _LAYER_III: 1152},
    _MPEG_V2: {_LAYER_I: 384, _LAYER_II: 1152, _LAYER_III: 576},
    _MPEG_V2_5: {_LAYER_I: 384, _LAYER_II: 1152, _LAYER_III: 576},
}

# Xing/Info sits inside the first frame, past the side information block.
_XING_MAGIC = (b"Xing", b"Info")
_XING_FRAMES_FLAG = 0x0001
_XING_SEARCH_BYTES = 200                # side info is at most ~36 bytes; be generous
_SYNC_SEARCH_BYTES = 8192               # tolerate junk before the first frame

_WAV_RIFF_MAGIC = b"RIFF"
_WAV_WAVE_MAGIC = b"WAVE"
_WAV_HEADER_BYTES = 12
_WAV_CHUNK_HEADER = struct.Struct("<4sI")
_WAV_BYTE_RATE_OFFSET = 8               # into the fmt chunk body
_DURATION_DECIMALS = 2


def _syncsafe(data: bytes) -> int:
    size = 0
    for byte in data:
        size = (size << _SYNCSAFE_BITS) | (byte & _SYNCSAFE_MASK)
    return size


def _id3_offset(data: bytes) -> int:
    """Bytes of ID3v2 tag to skip before the first audio frame."""
    if data[:len(_ID3_MAGIC)] != _ID3_MAGIC or len(data) < _ID3_HEADER_BYTES:
        return 0
    flags = data[5]
    offset = _ID3_HEADER_BYTES + _syncsafe(data[6:10])
    if flags & _ID3_FOOTER_FLAG:
        offset += _ID3_HEADER_BYTES
    return offset


class _Frame:
    """One decoded MPEG audio frame header."""

    __slots__ = ("version", "layer", "bitrate", "sample_rate", "samples", "padding")

    def __init__(self, header: int):
        self.version = (header >> 19) & 0b11
        self.layer = (header >> 17) & 0b11
        bitrate_index = (header >> 12) & 0b1111
        sample_rate_index = (header >> 10) & 0b11
        self.padding = (header >> 9) & 0b1

        if (self.version == _MPEG_RESERVED or self.layer == _LAYER_RESERVED
                or bitrate_index in (_BITRATE_RESERVED_INDEX, _BITRATE_BAD_INDEX)
                or sample_rate_index == _SAMPLE_RATE_RESERVED_INDEX):
            raise ValueError("reserved or free-format frame header")

        table = _BITRATES_V1 if self.version == _MPEG_V1 else _BITRATES_V2
        self.bitrate = table[self.layer][bitrate_index] * _BITS_PER_KILOBIT
        self.sample_rate = _SAMPLE_RATES[self.version][sample_rate_index]
        self.samples = _SAMPLES_PER_FRAME[self.version][self.layer]
        if not self.bitrate or not self.sample_rate:
            raise ValueError("frame header declares no bitrate or sample rate")

    @property
    def side_info_end(self) -> int:
        """Where a Xing/Info tag would start, relative to the frame."""
        return _FRAME_HEADER_BYTES


def _find_frame(data: bytes, start: int) -> tuple[int, _Frame]:
    limit = min(len(data) - _FRAME_HEADER_BYTES, start + _SYNC_SEARCH_BYTES)
    position = start
    while position <= limit:
        header = int.from_bytes(data[position:position + _FRAME_HEADER_BYTES], "big")
        if (header >> 16) & _FRAME_SYNC_MASK == _FRAME_SYNC_VALUE:
            try:
                return position, _Frame(header)
            except ValueError:
                pass
        position += 1
    raise ValueError("no MPEG audio frame found")


def _xing_frame_count(data: bytes, frame_start: int, frame: _Frame) -> int | None:
    """VBR frame count, when the encoder wrote one. Exact where CBR maths is not."""
    window_start = frame_start + frame.side_info_end
    window = data[window_start:window_start + _XING_SEARCH_BYTES]
    for magic in _XING_MAGIC:
        index = window.find(magic)
        if index < 0:
            continue
        cursor = window_start + index + len(magic)
        flags = int.from_bytes(data[cursor:cursor + 4], "big")
        if not flags & _XING_FRAMES_FLAG:
            continue
        cursor += 4
        count = int.from_bytes(data[cursor:cursor + 4], "big")
        if count > 0:
            return count
    return None


def _mp3_seconds(data: bytes) -> float:
    offset = _id3_offset(data)
    frame_start, frame = _find_frame(data, offset)

    frames = _xing_frame_count(data, frame_start, frame)
    if frames:
        return frames * frame.samples / frame.sample_rate

    # Constant-bitrate fallback: the audio payload divided by its own bitrate.
    audio_bytes = len(data) - frame_start
    return audio_bytes * 8 / frame.bitrate


def _wav_seconds(data: bytes) -> float:
    if data[:4] != _WAV_RIFF_MAGIC or data[8:12] != _WAV_WAVE_MAGIC:
        raise ValueError("not a RIFF/WAVE payload")
    cursor, byte_rate = _WAV_HEADER_BYTES, None
    while cursor + _WAV_CHUNK_HEADER.size <= len(data):
        name, size = _WAV_CHUNK_HEADER.unpack_from(data, cursor)
        body = cursor + _WAV_CHUNK_HEADER.size
        if name == b"fmt " and size >= _WAV_BYTE_RATE_OFFSET + 4:
            byte_rate = struct.unpack_from("<I", data, body + _WAV_BYTE_RATE_OFFSET)[0]
        elif name == b"data" and byte_rate:
            return size / byte_rate
        cursor = body + size + (size & 1)          # chunks are word-aligned
    raise ValueError("no data chunk with a usable byte rate")


def seconds(data: bytes) -> float | None:
    """Measured duration of an audio payload, or None if it cannot be read."""
    if not data:
        return None
    try:
        if data[:4] == _WAV_RIFF_MAGIC:
            value = _wav_seconds(data)
        else:
            value = _mp3_seconds(data)
    except Exception:  # noqa: BLE001 - an unreadable duration must never fail a paid job
        return None
    if value <= 0:
        return None
    return round(value, _DURATION_DECIMALS)


def seconds_of_file(path: Path) -> float | None:
    try:
        return seconds(Path(path).read_bytes())
    except OSError:
        return None
