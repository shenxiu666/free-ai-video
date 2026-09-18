"""Planner 人名 grounding 回归：原文原形 + 角色库双重校验 + 分镜对齐."""

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

import planner as pl  # noqa: E402

SRC = "阿雪雪夜下山，donk 随行。教练 hally 在山口等待，CEO 致电祝贺。"

LIB = [
    {"name": "阿雪", "aliases": ["雪姑娘"]},
    {"name": "donk", "aliases": ["王牌"]},
    {"name": "hally", "aliases": ["教练"]},
]


def _plan(names):
    import math
    total = 24
    need = max(1, math.ceil(total / 15))
    return {
        "total_seconds": total,
        "clip_durations": [6, 8, 10],
        "style_effective": "写实",
        "cast_plan": [{"name": n, "role": "主角", "episodes": ["E01"]} for n in names],
        "market": {"hook_3s": "钩子", "beats": ["b%d" % i for i in range(need)],
                   "cliffhanger": "悬念", "risk": ""},
        "density": {"chars_per_sec": 10, "dialogue_max": 30},
    }


def test_prompt_requires_verbatim_and_anchor_names():
    _, auto = pl.build_planner_prompt("剧", None, "写实", SRC)
    _, manual = pl.build_planner_prompt("剧", 24, "写实", SRC)
    for user in (auto, manual):
        assert "逐字出现" in user
        assert "只许用锚点清单中的名字" in user
        assert "人名必须出自原文" in user


def test_coerce_rejects_invented_name():
    with pytest.raises(ValueError, match="不在原文中"):
        pl.coerce_plan(_plan(["阿雪", "秦墨"]), None, "写实",
                       source_text=SRC, library_names=None)


def test_coerce_rejects_name_missing_from_library():
    # 在原文但不在库 → 库非空时拒绝
    with pytest.raises(ValueError, match="不在角色库中"):
        pl.coerce_plan(_plan(["阿雪", "CEO"]), None, "写实",
                       source_text=SRC, library_names=LIB)


def test_coerce_canonicalizes_alias_to_library_name():
    plan = pl.coerce_plan(_plan(["教练", "donk"]), None, "写实",
                          source_text=SRC, library_names=LIB)
    names = [c["name"] for c in plan["cast_plan"]]
    assert names == ["hally", "donk"]


def test_coerce_skips_checks_when_no_source_nor_library():
    # 旧透传行为：无 grounding 入参不断链
    plan = pl.coerce_plan(_plan(["阿雪", "秦墨"]), 24, "写实")
    assert [c["name"] for c in plan["cast_plan"]] == ["阿雪", "秦墨"]
    # 空库跳过库检，只做原文检
    plan2 = pl.coerce_plan(_plan(["阿雪", "CEO"]), None, "写实",
                           source_text=SRC, library_names=[])
    assert [c["name"] for c in plan2["cast_plan"]] == ["阿雪", "CEO"]


def test_reconcile_script_cast():
    import main as m
    script = {"clips": [
        {"id": "s01", "cast": ["教练", "路人甲"]},
        {"id": "s02", "cast": ["阿雪"]},
    ]}
    remapped, kept = m._reconcile_script_cast(script, LIB)
    assert remapped == 1
    assert kept == 1
    assert script["clips"][0]["cast"] == ["hally", "路人甲"]
    assert script["clips"][1]["cast"] == ["阿雪"]
    # 空库不打扰
    script2 = {"clips": [{"id": "s01", "cast": ["谁"]}]}
    assert m._reconcile_script_cast(script2, []) == (0, 0)
    assert script2["clips"][0]["cast"] == ["谁"]
