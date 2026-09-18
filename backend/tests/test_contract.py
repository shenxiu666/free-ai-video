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
    # M3 契约放宽：下限由 60s 改为 >0；原 30s 场景移至 test_script_passes_short_total_30
    with pytest.raises(ValueError):
        validate_script(_valid_script(total_seconds=0))


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


# ---- B 路线：narration/dialogue/speaker 拆分 ----
def test_script_rejects_empty_narration_and_dialogue():
    s = _valid_script()
    s["clips"][0]["narration"] = ""
    with pytest.raises(ValueError) as exc:
        validate_script(s)
    assert "s00" in str(exc.value)  # 点名 clip id


def test_script_passes_dialogue_only():
    s = _valid_script()
    s["clips"][0]["narration"] = ""
    s["clips"][0]["dialogue"] = "人物原话"
    s["clips"][0]["speaker"] = "阿雪"
    assert validate_script(s) is True


def test_script_passes_legacy_narration_only():
    # 老剧无新字段按旁白兼容：只有 narration、无 dialogue/speaker 也通过
    s = _valid_script()
    assert "dialogue" not in s["clips"][0] and "speaker" not in s["clips"][0]
    assert validate_script(s) is True


def test_script_passes_long_dialogue():
    # B路线：超长对白不截断不阻断（拆镜由前端警告/P1 prompt截断处理）
    s = _valid_script()
    s["clips"][0]["narration"] = ""
    s["clips"][0]["dialogue"] = "台" * 200
    assert validate_script(s) is True


# ---- M3 契约放宽：total>0 / clip_seconds auto / cast 透传 ----
def _script_with_durations(total, durations, clip_seconds="8", **over):
    s = _valid_script(total_seconds=total, clip_seconds=clip_seconds, **over)
    s["clips"] = [
        {"id": f"s{i:02d}", "duration": d, "narration": f"台词{i}"}
        for i, d in enumerate(durations)
    ]
    return s


def test_script_passes_short_total_15():
    assert validate_script(_script_with_durations(15, [8, 7])) is True


def test_script_passes_short_total_30():
    assert validate_script(_script_with_durations(30, [8, 8, 8, 6])) is True


def test_script_passes_clip_seconds_auto():
    s = _script_with_durations(16, [8, 8], clip_seconds="auto")
    assert validate_script(s) is True


def test_script_rejects_auto_with_bad_duration():
    # auto 下逐镜 duration 仍须各自 4-12
    with pytest.raises(ValueError):
        validate_script(_script_with_durations(11, [8, 3], clip_seconds="auto"))
    with pytest.raises(ValueError):
        validate_script(_script_with_durations(21, [8, 13], clip_seconds="auto"))


def test_script_passes_cast_passthrough():
    s = _valid_script()
    s["clips"][0]["cast"] = ["阿雪", "旁白"]
    assert validate_script(s) is True
    # 缺省 cast 的老剧同样通过
    assert validate_script(_valid_script()) is True


def test_script_rejects_bad_cast():
    s = _valid_script()
    s["clips"][0]["cast"] = ["阿雪", 123]
    with pytest.raises(ValueError):
        validate_script(s)
    s["clips"][0]["cast"] = "阿雪"  # 须为数组
    with pytest.raises(ValueError):
        validate_script(s)


# ---- 生成模式媒体三态 + scene/scene_refs（AI 规划模式） ----
def test_script_passes_planned_modes_and_scene():
    s = _valid_script()
    s["scene_refs"] = ["scenes/s01.png"]
    s["clips"][0].update({"video_mode": "reference",
                          "images": ["characters/c01.png"],
                          "cast": ["阿雪"], "scene": ["雪夜山门"]})
    s["clips"][1].update({"video_mode": "keyframe",
                          "first_frame": "images/s01.png"})
    assert validate_script(s) is True


def test_script_rejects_bad_scene_and_media_combo():
    s = _valid_script()
    s["clips"][0]["scene"] = "雪夜山门"  # 须为数组
    with pytest.raises(ValueError):
        validate_script(s)
    s = _valid_script()
    s["scene_refs"] = [f"s{i}.png" for i in range(6)]  # ≤5
    with pytest.raises(ValueError):
        validate_script(s)
    s = _valid_script()
    s["clips"][0].update({"video_mode": "text", "images": ["a.png"]})
    with pytest.raises(ValueError):  # text 禁媒体
        validate_script(s)
    s = _valid_script()
    s["clips"][0].update({"video_mode": "keyframe"})  # 需首尾帧其一
    with pytest.raises(ValueError):
        validate_script(s)
    s = _valid_script()
    s["clips"][0].update({"video_mode": "reference"})  # 需图/音其一
    with pytest.raises(ValueError):
        validate_script(s)
    s = _valid_script()
    s["clips"][0].update({"video_mode": "reference",
                          "images": ["a.png"], "videos": ["v.mp4"]})
    with pytest.raises(ValueError):  # reference 禁 videos
        validate_script(s)
    # 只验形态不验存在性：幻造路径照样通过（缺图走补齐闭环，不阻断落盘）
    s = _valid_script()
    s["clips"][0].update({"video_mode": "reference",
                          "images": ["characters/c99_ghost.png"]})
    assert validate_script(s) is True


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
