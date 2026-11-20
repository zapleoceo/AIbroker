"""The typed speech result is always canonical PCM WAV, never raw guessed audio."""
import io
import struct
import wave

import pytest

from aibroker.services.speech_wav import _canonical_wav


def _wav() -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24_000)
        audio.writeframes(b"\xff\x7f\x00\x80")
    return out.getvalue()


def _riff(data: bytes) -> bytes:
    return b"RIFF" + struct.pack("<I", len(data) + 4) + b"WAVE" + data


def test_wav_is_canonical_and_ancillary_chunk_is_stripped():
    original = _wav()
    extra = _riff(b"JUNK" + struct.pack("<I", 3) + b"odd\0" + original[12:])
    output = _canonical_wav(extra, "audio/x-wav", max_bytes=1024)
    assert output == original
    with wave.open(io.BytesIO(output), "rb") as sample:
        assert (sample.getnchannels(), sample.getframerate(), sample.getsampwidth()) == (1, 24000, 2)
        assert sample.readframes(2) == b"\xff\x7f\x00\x80"


@pytest.mark.parametrize(("data", "mime", "limit"), [
    (_wav(), "audio/L16", 1024),
    (_wav(), None, 1024),
    (_wav(), "audio/wav", 44),
    (_wav()[:-1], "audio/wav", 1024),
    (_wav() + b"junk", "audio/wav", 1024),
    (b"RIFX" + _wav()[4:], "audio/wav", 1024),
    (_riff(_wav()[12:36] + _wav()[12:]), "audio/wav", 1024),
    (_riff(_wav()[12:] + b"data\x02\0\0\0x\0"), "audio/wav", 1024),
    (_riff(_wav()[12:36] + b"data\x03\0\0\0odd\0"), "audio/wav", 1024),
    (_riff(b"JUNK\xff\xff\xff\xff" + _wav()[12:]), "audio/wav", 1024),
])
def test_wav_rejects_unknown_or_structurally_invalid_audio(data, mime, limit):
    assert _canonical_wav(data, mime, max_bytes=limit) is None
