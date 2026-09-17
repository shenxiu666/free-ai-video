"""最小契约测试（Builder E）：script 校验 / video mode 校验 / image size 校验 / text 留空拒绝。"""

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from orchestrator import create_drama, get_state, retry_clip, validate_script  # noqa: E402
from providers.agnes_image import build_image_payload, generate_image  # noqa: E402
from providers.agnes_video import (  # noqa: E402
    build_video_payload,
    validate_video_params,
)
from providers.text import TextNotConfigured, generate_text  # noqa: E402


def _valid_script(**over):
    clips = [
        {"id": f"s{i:02d}", "duration": 8, "narration": f"台词{i}"}
        for i in range(8)
    ]
    script = {
        "title": "t",
        "total_seconds": 64,
        "aspect": "9:16",
        "clip_seconds": "8",
        "character_refs": ["c1.png"],
        "clips": clips,
    }
    script.update(over)
    return script


# ---- script.json 唯一真源校验 ----
def test_script_valid_passes(tmp_path):
    assert validate_script(_valid_script()) is True
    state = create_drama("demo", _valid_script(), base_dir=tmp_path)
    assert state["clips"]["s00"]["video"] == "pending"
    assert get_state("demo", base_dir=tmp_path)["drama"] == "demo"
    retry_clip("demo", "s00", base_dir=tmp_path)  # 无 failed 也不报错


def test_script_rejects_short_total():
    with pytest.raises(ValueError):
        validate_script(_valid_script(total_seconds=30))


def test_script_rejects_sum_mismatch():
    s = _valid_script()
    s["clips"][0]["duration"] = 9
    with pytest.raises(ValueError):
        validate_script(s)


def test_script_rejects_too_many_refs():
    with pytest.raises(ValueError):
        validate_script(_valid_script(character_refs=[f"c{i}" for i in range(6)]))


def test_script_rejects_bad_clip_seconds():
    with pytest.raises(ValueError):
        validate_script(_valid_script(clip_seconds="3"))
    with pytest.raises(ValueError):
        validate_script(_valid_script(clip_seconds=8))  # 须为字符串


# ---- image size 校验 ----
def test_image_rejects_bad_size_ratio():
    with pytest.raises(ValueError):
        generate_image("p", size="720P", ratio="9:16", api_key="sk-test")
    with pytest.raises(ValueError):
        generate_image("p", size="1K", ratio="1:2", api_key="sk-test")


def test_image_payload_has_no_top_level_format_or_tags():
    payload = build_image_payload("主体测试", "1K", "9:16")
    assert payload["extra_body"] == {"response_format": "url"}
    assert "response_format" not in payload
    assert "tags" not in payload


# ---- video mode 校验 ----
def test_video_text_mode_forbids_media():
    with pytest.raises(ValueError):
        validate_video_params("text", "8", images=["a.png"])
    with pytest.raises(ValueError):
        validate_video_params("text", "8", first_frame="f.png")


def test_video_keyframe_needs_frame():
    with pytest.raises(ValueError):
        validate_video_params("keyframe", "8")
    validate_video_params("keyframe", "8", first_frame="f.png")  # 合法不抛


def test_video_reference_rules():
    with pytest.raises(ValueError):
        validate_video_params("reference", "8", videos=["v.mp4"])  # 禁 videos
    with pytest.raises(ValueError):
        validate_video_params("reference", "8", images=[f"{i}.png" for i in range(6)])
    with pytest.raises(ValueError):
        validate_video_params("reference", "8")  # 至少一类非空
    p = build_video_payload("动", "8", mode="reference", images=["a.png"])
    assert p["images"] == ["a.png"] and "videos" not in p


def test_video_seconds_must_be_str():
    with pytest.raises(ValueError):
        validate_video_params("text", 8)
    with pytest.raises(ValueError):
        validate_video_params("text", "3")


# ---- text 留空拒绝 ----
def test_text_unconfigured_rejects():
    with pytest.raises(TextNotConfigured):
        generate_text("写大纲", provider="", model="")
    with pytest.raises(TextNotConfigured):
        generate_text("写大纲", provider="agnes", model="")


def test_text_manual_fallback_by_default():
    # fallback 默认 manual：主键失败直接抛，不自动降级
    with pytest.raises(Exception):
        generate_text(
            "hi", provider="openai-compatible", model="m",
            base_url="http://127.0.0.1:9", api_key="sk-x",
            timeout=1, fallback_mode="manual",
            fallback={"provider": "agnes", "model": "agnes-3.0-flash",
                      "api_key": "sk-y"},
        )
