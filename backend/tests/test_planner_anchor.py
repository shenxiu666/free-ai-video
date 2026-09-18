"""Planner 去锚回归：prompt 纯 Schema 无示例数值 + auto 模式 fail fast/拒全等."""

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

import planner as pl  # noqa: E402


def _good_auto(total, durs):
    import math
    need = max(1, math.ceil(float(total) / 15))
    return {
        "total_seconds": total,
        "clip_durations": list(durs),
        "style_effective": "写实",
        "cast_plan": [],
        "market": {
            "hook_3s": "钩子",
            "beats": ["b%d" % i for i in range(need)],
            "cliffhanger": "悬念",
            "risk": "",
        },
        "density": {"chars_per_sec": 10, "dialogue_max": 30},
    }


def test_auto_prompt_has_no_numeric_example():
    _, user = pl.build_planner_prompt("剧", None, "写实", "原文" * 50)
    for lit in ('"beats_per_15s": 1.0', '"chars_per_sec": 10',
                '"dialogue_max": 30', "[8, 8]", "[8,8]"):
        assert lit not in user
    assert "纯 Schema" in user or "无示例数值" in user
    assert "两种不同秒数" in user


def test_explicit_prompt_density_has_no_numeric_example():
    _, user = pl.build_planner_prompt("剧", 60, "写实", "原文" * 50)
    for lit in ('"beats_per_15s": 1.0', '"chars_per_sec": 10',
                '"dialogue_max": 30'):
        assert lit not in user
    # 显式 total 本体允许出现（用户指定），但镜表/密度须为占位
    assert '"total_seconds": 60' in user


def test_coerce_auto_missing_durations_fails():
    raw = _good_auto(24, [6, 8, 10])
    del raw["clip_durations"]
    with pytest.raises(ValueError, match="clip_durations 缺失"):
        pl.coerce_plan(raw, None, "写实")


def test_coerce_auto_uniform_rejected():
    with pytest.raises(ValueError, match="全等凑整"):
        pl.coerce_plan(_good_auto(24, [8, 8, 8]), None, "写实")


def test_coerce_auto_diverse_ok_and_explicit_compat():
    plan = pl.coerce_plan(_good_auto(24, [6, 8, 10]), None, "写实")
    assert plan["total_seconds"] == 24
    # 显式模式：全等合法（用户手工均匀切分），缺表仍兜底
    good60 = _good_auto(60, [8] * 7 + [4])
    assert pl.coerce_plan(good60, 60, "写实")["total_seconds"] == 60
    explicit_uniform = _good_auto(16, [8, 8])
    assert pl.coerce_plan(explicit_uniform, 16, "写实")["clip_durations"] == [8, 8]
    missing = _good_auto(16, [8, 8])
    del missing["clip_durations"]
    filled = pl.coerce_plan(missing, 16, "写实")
    assert abs(sum(float(d) for d in filled["clip_durations"]) - 16) < 1e-6
