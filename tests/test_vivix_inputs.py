"""A1/W1 structural and trusted-media rules from the documented REST contracts."""
import copy

import pytest

from aibroker.providers.vivix_inputs import validate_inputs
from aibroker.providers.vivix_media import MediaEvidence, validate_media

IMAGE = "https://example.test/image.png"
AUDIO = "https://example.test/audio.wav"
WORLD = {"jobs": [{"prompt": "mock", "duration": 5}]}
AVATAR = {"prompt": "mock", "first_frame_image": IMAGE, "audio": AUDIO}


@pytest.mark.parametrize("change", [
    {"duration": 4}, {"duration": 16}, {"duration": True}, {"duration": "5"},
    {"prompt": " "}, {"aspect_ratio": "2:1"}, {"output_format": "gif"},
    {"ref_images": [{"url": IMAGE}] * 9}, {"ref_audios": [{"url": AUDIO}] * 5},
    {"ref_videos": [{"url": AUDIO}] * 5}, {"ref_images": [{"file_id": "bad"}]},
    {"first_frame_image": IMAGE, "ref_audios": [{"url": AUDIO}]},
    {"last_frame_image": IMAGE, "ref_images": [{"url": IMAGE}]},
    {"ref_images": [{"url": "file:///etc/passwd"}]},
    {"ref_images": [{"url": "https://secret:token@example.test/i"}]}, {"mode": "text"},
])
def test_world_rejects_invalid_inputs(change):
    body = copy.deepcopy(WORLD)
    body["jobs"][0].update(change)
    with pytest.raises(ValueError):
        validate_inputs("vivix-w1", body)


@pytest.mark.parametrize("change", [{"seed": -1}, {"seed": 2147483648}, {"seed": True},
                                   {"pe_mode": "unknown"}, {"jobs": []},
                                   {"jobs": WORLD["jobs"] * 2}])
def test_world_root_rules(change):
    with pytest.raises(ValueError):
        validate_inputs("vivix-w1", {**WORLD, **change})


@pytest.mark.parametrize("model", ["vivix-w1", "vivix-w1-fast", "vivix-w1-ultrafast",
                                  "vivix-w1-auto", "vivix-w1:latest"])
def test_world_supported_shapes(model):
    validate_inputs(model, {**WORLD, "seed": 0, "pe_mode": "enhanced"})


@pytest.mark.parametrize("change", [
    {"audio_data": "YWJj"}, {"dialogue": "hello", "voice_id": "voice"},
    {"audio": None}, {"first_frame_image": "bad"}, {"voice_id": "not-audio-mode"},
    {"resolution": "1080p"}, {"unexpected": 1},
    {"audio": None, "audio_data": "not-base64"},
    {"audio": None, "dialogue": "hello"},
])
def test_avatar_voice_exclusivity(change):
    with pytest.raises(ValueError):
        validate_inputs("vivix-a1", {**AVATAR, **change})


def test_avatar_segments_and_silent_duration():
    body = {"first_frame_image": IMAGE, "segments": [
        {"mode": "a2v", "prompt": "listen", "duration_ms": 1000},
        {"mode": "ta2v", "prompt": "speak", "dialogue": "hi", "voice_id": "fixture"},
        {"mode": "a2v", "prompt": "reply", "audio_url": AUDIO}]}
    validate_inputs("vivix-a1:latest", body)
    for bad in ({**body, "prompt": "root"}, {**body, "audio": AUDIO},
                {**body, "segments": []}, {**body, "segments": body["segments"] * 7}):
        with pytest.raises(ValueError):
            validate_inputs("vivix-a1", bad)
    body["segments"][2]["duration_ms"] = 1000
    with pytest.raises(ValueError):
        validate_inputs("vivix-a1", body)


def test_avatar_tts_and_inline_audio():
    validate_inputs("vivix-a1", {"first_frame_image": IMAGE, "prompt": "talk", "dialogue": "hi",
                                 "voice_id": "fixture", "tts_config": {"speed": 1.0}})
    validate_inputs("vivix-a1", {"first_frame_image": IMAGE, "prompt": "talk", "audio_data": "YWJj"})


def test_media_evidence_bound_to_exact_url_and_kind():
    data = {"jobs": [{"prompt": "mock", "duration": 5, "ref_images": [{"url": IMAGE}]}]}
    image = MediaEvidence("image", 100, "fixture-inspection", width=100, height=100)
    validate_media("vivix-w1", data, {IMAGE: image})
    for evidence in ({}, {IMAGE + "?different": image}, {IMAGE: MediaEvidence("audio", 100, "fixture")}):
        with pytest.raises(ValueError):
            validate_media("vivix-w1", data, evidence)


@pytest.mark.parametrize("seconds", [0, 16, float("nan"), float("inf")])
def test_media_duration_limits(seconds):
    data = {"jobs": [{"prompt": "mock", "duration": 5, "ref_audios": [{"url": AUDIO}]}]}
    with pytest.raises(ValueError):
        validate_media("vivix-w1", data, {AUDIO: MediaEvidence("audio", 100, "fixture", duration_s=seconds)})


def test_media_totals_and_image_ratio():
    data = {"jobs": [{"prompt": "mock", "duration": 5, "ref_audios": [{"url": AUDIO}] * 2}]}
    with pytest.raises(ValueError):
        validate_media("vivix-w1", data, {AUDIO: MediaEvidence("audio", 100, "fixture", duration_s=11)})
    data["jobs"][0] = {"prompt": "mock", "duration": 5, "first_frame_image": IMAGE}
    with pytest.raises(ValueError):
        validate_media("vivix-w1", data, {IMAGE: MediaEvidence("image", 100, "fixture", width=500, height=100)})
