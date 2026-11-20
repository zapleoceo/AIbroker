"""Canonical format for typed queued speech; no codec or raw-PCM sniffing."""
from __future__ import annotations

import struct

_WAV_MIME = "audio/wav"
_WAV_MIME_TYPES = frozenset({_WAV_MIME, "audio/x-wav", "audio/wave"})
# WAVE_FORMAT_PCM, one channel, 24 kHz, 48 kB/s, 2 bytes/frame, signed 16-bit.
_WAV_PCM = (1, 1, 24_000, 48_000, 2, 16)
_MAX_CHUNKS = 1024


def _canonical_wav(data: object, mime: object, *, max_bytes: int) -> bytes | None:
    """Verify little-endian mono PCM WAV and strip all ancillary RIFF chunks."""
    if (not isinstance(data, bytes) or not isinstance(mime, str)
            or mime.strip().lower() not in _WAV_MIME_TYPES
            or not 44 <= len(data) <= max_bytes):
        return None
    if (data[:4] != b"RIFF" or data[8:12] != b"WAVE"
            or struct.unpack_from("<I", data, 4)[0] != len(data) - 8):
        return None
    at = 12
    fmt_seen = False
    pcm: bytes | None = None
    chunks = 0
    while at < len(data):
        chunks += 1
        if chunks > _MAX_CHUNKS or len(data) - at < 8:
            return None
        kind, length = struct.unpack_from("<4sI", data, at)
        start = at + 8
        end = start + length
        at = end + (length & 1)
        if at > len(data):
            return None
        if kind == b"fmt ":
            if (fmt_seen or length not in (16, 18)
                    or struct.unpack_from("<HHIIHH", data, start) != _WAV_PCM
                    or (length == 18 and data[start + 16:end] != b"\0\0")):
                return None
            fmt_seen = True
        elif kind == b"data":
            if not fmt_seen or pcm is not None or length == 0 or length % 2:
                return None
            pcm = data[start:end]
    if pcm is None:
        return None
    return (
        struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(pcm), b"WAVE",
                    b"fmt ", 16, *_WAV_PCM, b"data", len(pcm)) + pcm
    )
