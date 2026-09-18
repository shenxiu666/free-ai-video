"""M1 系列/规划/角色/可变分镜测试（docs/01.6、docs/04）。

覆盖：series CRUD、planner 时长求和、角色合并、breakdown 可变表回退。
API 侧用 fake 文本/生图通道，不碰真实 Key 与网络。
"""

import base64
import sys
import types
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

import breakdown as bd  # noqa: E402
import characters as ch  # noqa: E402
import planner as pl  # noqa: E402
import series as se  # noqa: E402


# ---------- series CRUD ----------

def test_series_crud_and_total_is_sum(tmp_path):
    s = se.create_series("  测剧  ", base_dir=tmp_path, meta={"title": "测剧"})
    assert s["name"] == "测剧"
    assert s["total_seconds"] == 0
    e1 = se.create_episode("测剧", title="第一集", source_text="原文一" * 100,
                           total_seconds=60, base_dir=tmp_path)
    assert e1["id"] == "E01"
    assert e1["source_chars"] == len("原文一" * 100)
    assert e1["truncated"] is False
    assert len(e1["source_hash"]) == 64
    e2 = se.create_episode("测剧", title="第二集", source_text="原文二" * 10,
                           total_seconds=90, base_dir=tmp_path)
    assert e2["id"] == "E02"
    got = se.get_series("测剧", base_dir=tmp_path)
    assert got["total_seconds"] == pytest.approx(150.0)
    assert got["episode_count"] == 2
    assert [e["id"] for e in got["episodes"]] == ["E01", "E02"]
    # 系列总时长=各集之和：改一集后自动重算
    se.update_episode("测剧", "E02", {"total_seconds": 60}, base_dir=tmp_path)
    assert se.series_total_seconds("测剧", base_dir=tmp_path) == pytest.approx(120.0)
    # PATCH 标题
    patched = se.update_episode("测剧", "E01", {"title": "新名"}, base_dir=tmp_path)
    assert patched["title"] == "新名"
    # source.md 落盘且超长截断标记
    long_text = "长" * 13000
    e3 = se.create_episode("测剧", title="长集", source_text=long_text,
                           total_seconds=60, base_dir=tmp_path)
    assert e3["truncated"] is True
    assert e3["source_chars"] == 13000
    stored = (tmp_path / "测剧" / "episodes" / e3["id"] / "source.md").read_text(encoding="utf-8")
    assert len(stored) == 12000
    # DELETE 分集后总量回落
    se.delete_episode("测剧", e3["id"], base_dir=tmp_path)
    assert se.series_total_seconds("测剧", base_dir=tmp_path) == pytest.approx(120.0)
    # list/update/delete 系列
    assert any(x["name"] == "测剧" for x in se.list_series(base_dir=tmp_path))
    upd = se.update_series("测剧", {"title": "新剧名"}, base_dir=tmp_path)
    assert upd["title"] == "新剧名"
    assert se.delete_series("测剧", base_dir=tmp_path) is True
    with pytest.raises(FileNotFoundError):
        se.get_series("测剧", base_dir=tmp_path)


def test_series_sanitize_reuses_orchestrator_logic(tmp_path):
    # 非法字符 \/:*?"<>| 被过滤（与 orchestrator._drama_dir 同逻辑）
    s = se.create_series('a/b\\c:d*e?f"g<h>i|j', base_dir=tmp_path)
    assert s["name"] == "abcdefghij"
    assert "/" not in s["name"] and "\\" not in s["name"]
    for bad in (':', '*', '?', '"', '<', '>', '|'):
        assert bad not in s["name"]
    p = se.series_dir('a/b\\c:d*e?f"g<h>i|j', base_dir=tmp_path)
    assert p.is_dir()
    with pytest.raises(ValueError):
        se.create_series('   \\/:*?"<>|   ', base_dir=tmp_path)
    with pytest.raises(ValueError):
        se.normalize_episode_id("EX")
    assert se.normalize_episode_id("1") == "E01"
    assert se.normalize_episode_id("e2") == "E02"
    assert se.normalize_episode_id("E03") == "E03"


def test_series_upload_decode_and_prepare(tmp_path):
    assert se.decode_upload_bytes("你好".encode("utf-8")) == "你好"
    assert se.decode_upload_bytes("你好".encode("gbk")) == "你好"
    prep = se.prepare_source("x" * 13000)
    assert prep["truncated"] is True and len(prep["stored"]) == 12000
    assert prep["source_chars"] == 13000


# ---------- planner 时长求和 ----------

def test_planner_durations_sum_and_range():
    for total in (60, 64, 65, 90, 120):
        durs = pl.clip_durations_for_total(total)
        assert abs(sum(float(d) for d in durs) - total) < 1e-6
        for i, d in enumerate(durs):
            f = float(d)
            assert 4 <= f <= 12, (total, durs)
            if i < len(durs) - 1:
                assert float(d).is_integer(), (total, durs)


def test_planner_coerce_ok_and_rejects():
    good = {
        "total_seconds": 60,
        "clip_durations": [8] * 7 + [4],
        "style_effective": "写实",
        "cast_plan": [{"name": "阿雪", "role": "主角"}],
        "market": {"hook_3s": "钩子", "beats": ["b1", "b2", "b3", "b4"],
                   "beats_per_15s": 1.0, "cliffhanger": "悬念", "risk": ""},
        "density": {"chars_per_sec": 10, "dialogue_max": 30},
    }
    plan = pl.coerce_plan(good, 60, "写实")
    assert plan["total_seconds"] == 60
    assert abs(sum(float(d) for d in plan["clip_durations"]) - 60) < 1e-6
    assert pl.validate_plan(plan) is True
    # 缺 hook_3s
    bad = {**good, "market": {**good["market"], "hook_3s": "  "}}
    with pytest.raises(ValueError):
        pl.coerce_plan(bad, 60)
    # beats 不足（60s 至少 4 个）
    bad2 = {**good, "market": {**good["market"], "beats": ["only"]}}
    with pytest.raises(ValueError):
        pl.coerce_plan(bad2, 60)
    # 无悬念
    bad3 = {**good, "market": {**good["market"], "cliffhanger": ""}}
    with pytest.raises(ValueError):
        pl.coerce_plan(bad3, 60)
    # 求和不等
    bad4 = {**good, "clip_durations": [8] * 7 + [5]}
    with pytest.raises(ValueError):
        pl.coerce_plan(bad4, 60)
    # 非末镜小数
    bad5 = {**good, "clip_durations": [8.5] + [8] * 6 + [4.5]}
    with pytest.raises(ValueError):
        pl.coerce_plan(bad5, 60)
    # density 越界
    bad6 = {**good, "density": {"chars_per_sec": 20, "dialogue_max": 30}}
    with pytest.raises(ValueError):
        pl.coerce_plan(bad6, 60)
    bad7 = {**good, "density": {"chars_per_sec": 10, "dialogue_max": 45}}
    with pytest.raises(ValueError):
        pl.coerce_plan(bad7, 60)


def test_planner_prompt_rules_and_split():
    system, user = pl.build_planner_prompt("剧", 60, "写实", "原文" * 50)
    assert "hook_3s" in user or "钩子" in user
    assert "15s" in user and "悬念" in user
    assert "45" in user  # 超长对白>45必须拆镜写进题面
    assert pl.needs_split("台" * 46) is True
    assert pl.needs_split("台" * 45) is False
    parts = pl.split_dialogue("台" * 100, limit=45)
    assert "".join(parts) == "台" * 100
    assert all(len(p) <= 45 for p in parts)


# ---------- 角色合并 ----------

def _char(name, **kw):
    base = {"id": "c01", "name": name, "aliases": [], "logline": "",
            "appearance": "短发", "personality": "冷静", "images": [],
            "voice": {"provider": "", "voice_id": ""},
            "first_seen": "E01", "notes": "", "locked": False}
    base.update(kw)
    return base


def test_characters_merge_locked_and_alias():
    existing = [_char("阿雪", id="c01", appearance="短发少女",
                      aliases=["雪姑娘"], locked=True)]
    incoming = [_char("雪姑娘", id="c99", appearance="长发女王",
                       aliases=["阿雪"], images=["characters/c99_x.png"])]
    merged = ch.merge_characters(existing, incoming)
    assert len(merged) == 1  # 同名/别名合并，不新增
    assert merged[0]["appearance"] == "短发少女"  # locked 不覆盖手改
    assert "characters/c99_x.png" in merged[0]["images"]  # 立绘仍并入
    # 非锁定也不覆盖：只填空（加法合并，有新增就增加，没有就不变）
    existing2 = [_char("阿牛", id="c01", appearance="旧", locked=False)]
    incoming2 = [_char("阿牛", appearance="新外貌")]
    merged2 = ch.merge_characters(existing2, incoming2)
    assert merged2[0]["appearance"] == "旧"
    # 空字段可被补齐
    existing2b = [_char("阿牛", id="c01", appearance="")]
    merged2b = ch.merge_characters(existing2b, incoming2)
    assert merged2b[0]["appearance"] == "新外貌"
    # first_seen 只提前不后移
    existing2c = [_char("阿牛", id="c01", first_seen="E01")]
    incoming2c = [_char("阿牛", first_seen="E03")]
    assert ch.merge_characters(existing2c, incoming2c)[0]["first_seen"] == "E01"
    # 全新角色追加
    merged3 = ch.merge_characters(existing2, [_char("路人", id="c02")])
    assert len(merged3) == 2


def test_characters_validate_appearance_limit():
    bad = _char("X", appearance="字" * 201)
    with pytest.raises(ValueError):
        ch.validate_character(bad)
    ok_prompt = ch.build_portrait_prompt(_char("阿雪", appearance="短发少女"),
                                         style="写实")
    for seg in ("[主体]", "[场景]", "[风格]", "[光照]", "[构图]", "[质量]"):
        assert seg in ok_prompt


def test_characters_prompt_and_coerce():
    system, user = ch.build_character_prompt("原文" * 100)
    assert "characters" in user
    raw = {"characters": [{"name": "阿雪", "appearance": "短发",
                           "personality": "冷静"}]}
    out = ch.coerce_characters(raw, first_seen="E01")
    assert out[0]["id"] == "c01" and out[0]["first_seen"] == "E01"
    assert ch.summarize_characters(out) != ""


# ---------- breakdown 可变表回退 ----------

def test_breakdown_variable_durations_and_fallback():
    durs = [8] * 7 + [4]
    s1, u1 = bd.build_breakdown_prompt("剧", 60, "9:16", "8", "写实", "原文",
                                       durations=durs, style_effective="赛博",
                                       characters_summary="- 阿雪：短发",
                                       prev_summary="前" * 500)
    assert "- 8" in u1 and "赛博" in u1
    assert "- 阿雪" in u1  # 角色摘要注入
    assert "前集梗概" in u1
    prev_block = u1.split("【前集梗概")[1].split("【原文】")[0]
    assert len(prev_block.strip()) <= 400  # 300字截断+标签余量内
    # 无参回退现有均匀逻辑：60s/8s => 7x8+4
    s0, u0 = bd.build_breakdown_prompt("剧", 60, "9:16", "8", "写实", "原文")
    assert u0.count("- 8") >= 7
    raw = {"clips": [{"narration": f"旁白{i}",
                      "image_prompt": "[主体]x+[场景]x+[风格]x+[光照]x+[构图]x+[质量]1K",
                      "video_prompt": "[主体]x+[动作]x+[场景]x+[运镜]x+[光照]x+[风格]x"}
                     for i in range(8)]}
    sc_var = bd.coerce_script(raw, "剧", 60, "9:16", "8", "写实", durations=durs)
    assert [c["duration"] for c in sc_var["clips"]] == durs
    sc_old = bd.coerce_script(raw, "剧", 60, "9:16", "8", "写实")
    assert [c["duration"] for c in sc_old["clips"]] == bd.clip_plan(60, "8")
    assert abs(sum(c["duration"] for c in sc_old["clips"]) - 60) < 1e-6


# ---------- API ----------

class _FakePool:
    def __init__(self, kind_ok=("text", "image")):
        self.released = []
        self.kind_ok = kind_ok

    def acquire(self, kind, **kw):
        assert kind in self.kind_ok
        return types.SimpleNamespace(id="k1", raw_key="sk-fake-image-key-12345")

    def release(self, key, ok, err=None, cost=1, kind="text", latency_ms=0.0):
        self.released.append({"ok": ok, "kind": kind})
        return "ok"


def _api_client(monkeypatch):
    from fastapi.testclient import TestClient

    import main as m

    pool = _FakePool()
    monkeypatch.setattr(m, "_pool", lambda: pool)
    monkeypatch.setattr(m, "_text_cfg", lambda: {
        "provider": "agnes", "model": "agnes-3.0-flash", "base_url": "",
        "temperature": 0.7, "max_tokens": 8192, "timeout_s": 30,
        "fallback": {"mode": "manual"}})
    return TestClient(m.app), m, pool


def _canned_plan():
    # 去锚/grounding：假数据 cast 置空（各用例原文多为灌水字，无真名可 grounding；
    # 需真名的用例自行覆写 cast_plan，见 test_c1_plan_ai_total_and_writeback_tmp）。
    return {"total_seconds": 60, "clip_durations": [8] * 7 + [4],
            "style_effective": "写实",
            "cast_plan": [],
            "market": {"hook_3s": "钩子", "beats": ["a", "b", "c", "d"],
                       "beats_per_15s": 1.0, "cliffhanger": "悬念", "risk": ""},
            "density": {"chars_per_sec": 10, "dialogue_max": 30}}


def _canned_script_clips(n=8, durs=None):
    durs = durs or ([8] * 7 + [4])
    clips = []
    cursor = 0.0
    for i, d in enumerate(durs):
        clips.append({"id": f"s{i + 1:02d}", "start": cursor, "duration": d,
                      "narration": f"第{i + 1}镜旁白",
                      "image_prompt": "[主体]少年+[场景]雪夜+[风格]写实+[光照]月光+[构图]竖构图+[质量]1K,高细节",
                      "video_prompt": "[主体]少年+[动作]抬头+[场景]雪夜+[运镜]缓慢推镜+[光照]月光+[风格]写实",
                      "video_mode": "text"})
        cursor += d
    return clips


def _cleanup_series(name):
    import shutil

    from pathlib import Path as _P
    root = _P(__file__).resolve().parent.parent.parent / "outputs" / name
    shutil.rmtree(root, ignore_errors=True)


def test_api_series_episode_patch_delete(monkeypatch):
    c, m, pool = _api_client(monkeypatch)
    name = "M1SERIESCRUD"
    try:
        r = c.post("/api/series/new", json={"name": name, "title": name})
        assert r.status_code == 200, r.text
        r = c.get("/api/series")
        assert r.status_code == 200 and any(x["name"] == name for x in r.json()["series"])
        r = c.post(f"/api/series/{name}/episodes", json={
            "title": "E1", "total_seconds": 60, "source_text": "原文" * 500})
        assert r.status_code == 200, r.text
        ep = r.json()["episode"]
        assert ep["id"] == "E01" and ep["truncated"] is False
        assert ep["source_chars"] == len("原文" * 500)
        r = c.get(f"/api/series/{name}")
        assert r.status_code == 200
        assert r.json()["series"]["total_seconds"] == pytest.approx(60.0)
        r = c.patch(f"/api/series/{name}/episodes/E01", json={"title": "新标题"})
        assert r.status_code == 200 and r.json()["episode"]["title"] == "新标题"
        # 超 12000 字截断标记
        r = c.post(f"/api/series/{name}/episodes", json={
            "title": "E2", "total_seconds": 60, "source_text": "长" * 13000})
        assert r.status_code == 200 and r.json()["episode"]["truncated"] is True
        r = c.delete(f"/api/series/{name}/episodes/E02")
        assert r.status_code == 200
        assert c.get(f"/api/series/{name}").json()["series"]["total_seconds"] == pytest.approx(60.0)
    finally:
        _cleanup_series(name)


def test_api_episode_upload_md_txt_limits(monkeypatch):
    c, m, pool = _api_client(monkeypatch)
    name = "M1SERIESUP"
    try:
        assert c.post("/api/series/new", json={"name": name}).status_code == 200
        # 原始字节上传（md/txt、<=2MB、UTF-8优先GBK回退，不依赖 python-multipart）
        r = c.post(f"/api/series/{name}/episodes?title=MD集&total_seconds=60&filename=chap.md",
                   content="第一章内容".encode("utf-8"),
                   headers={"Content-Type": "text/markdown"})
        assert r.status_code == 200, r.text
        assert r.json()["episode"]["source_chars"] == len("第一章内容")
        # 非法后缀
        r = c.post(f"/api/series/{name}/episodes?title=X&total_seconds=60&filename=a.exe",
                   content=b"xx", headers={"Content-Type": "application/octet-stream"})
        assert r.status_code == 400
        # 超 2MB
        r = c.post(f"/api/series/{name}/episodes?title=BIG&total_seconds=60&filename=big.md",
                   content=b"x" * (2 * 1024 * 1024 + 1),
                   headers={"Content-Type": "text/markdown"})
        assert r.status_code == 400
        # GBK 回退
        r = c.post(f"/api/series/{name}/episodes?title=GBK&total_seconds=60&filename=gbk.txt",
                   content="你好世界".encode("gbk"), headers={"Content-Type": "text/plain"})
        assert r.status_code == 200, r.text
        # multipart（若环境装了 python-multipart 则同步验证同一约束）
        try:
            import multipart  # noqa: F401
            has_mp = True
        except ImportError:
            try:
                import python_multipart  # noqa: F401
                has_mp = True
            except ImportError:
                has_mp = False
        if has_mp:
            r = c.post(f"/api/series/{name}/episodes",
                       files={"file": ("chap2.md", "第二章".encode("utf-8"), "text/markdown")},
                       data={"title": "MD2", "total_seconds": "60"})
            assert r.status_code == 200, r.text
    finally:
        _cleanup_series(name)


def test_api_plan_breakdown_and_portrait(monkeypatch):
    import json as _json

    c, m, pool = _api_client(monkeypatch)
    name = "M1SERIESFLOW"
    try:
        assert c.post("/api/series/new", json={"name": name}).status_code == 200
        r = c.post(f"/api/series/{name}/episodes", json={
            "title": "E1", "total_seconds": 60, "source_text": "少年雪夜下山阿雪雪姑娘" * 200})
        assert r.status_code == 200, r.text

        def _fake_text(prompt, **kw):
            if "策划" in prompt or "clip_durations" in prompt:
                return {"text": _json.dumps(_canned_plan(), ensure_ascii=False),
                        "generated_by": "fake/plan@now"}
            if "角色" in prompt and "characters" in prompt:
                return {"text": _json.dumps({"characters": [
                    {"name": "阿雪", "aliases": ["雪姑娘"],
                     "appearance": "短发少女", "personality": "冷静"}]},
                    ensure_ascii=False), "generated_by": "fake/ch@now"}
            clips = _canned_script_clips()
            return {"text": _json.dumps(
                {"title": "E1", "total_seconds": 60, "aspect": "9:16",
                 "resolution": "720x1280", "clip_seconds": "8",
                 "character_refs": [], "clips": clips}, ensure_ascii=False),
                "generated_by": "fake/bd@now"}

        monkeypatch.setattr(m, "_generate_text", _fake_text)
        # plan 落盘
        r = c.post(f"/api/series/{name}/plan", json={"episode_id": "E01", "style": "写实"})
        assert r.status_code == 200, r.text
        assert r.json()["plan"]["market"]["hook_3s"] == "钩子"
        # characters 抽取合并
        r = c.post(f"/api/series/{name}/characters/extract", json={"episode_id": "E01"})
        assert r.status_code == 200, r.text
        assert r.json()["count"] >= 1
        r = c.get(f"/api/series/{name}/characters")
        assert r.status_code == 200 and r.json()["count"] >= 1
        cid = r.json()["characters"][0]["id"]
        # breakdown 用 plan 可变表 + 角色/前集注入
        r = c.post(f"/api/series/{name}/episodes/E01/breakdown", json={})
        assert r.status_code == 200, r.text
        assert len(r.json()["script"]["clips"]) == 8
        # portrait：fake 生图 b64，不碰网络
        tiny_png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfakepng").decode()
        monkeypatch.setattr(m, "_generate_image",
                            lambda prompt, **kw: {"b64": tiny_png_b64,
                                                 "generated_by": "agnes/agnes-image-2.5-flash@now"})
        try:
            monkeypatch.setattr(m, "_M1_generate_image",
                                lambda prompt, **kw: {"b64": tiny_png_b64,
                                                     "generated_by": "agnes/agnes-image-2.5-flash@now"})
        except AttributeError:
            pass
        assert kw_check_portrait_payload(monkeypatch, m)
        r = c.post(f"/api/series/{name}/characters/{cid}/portrait", json={"style": "写实"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["path"].startswith("characters/")
        assert body["path"] in body["character"]["images"]
        assert "[主体]" in body["prompt"]
    finally:
        _cleanup_series(name)
        # breakdown 镜像的老命名空间目录一并清理
        import shutil as _sh

        _sh.rmtree(_repo_outputs() / f"{name} E01", ignore_errors=True)


def kw_check_portrait_payload(monkeypatch, m):
    """立绘通道约束：size 仅 1K，不传 tags，response_format 禁顶层（走真 payload 校验）。"""
    from providers.agnes_image import build_image_payload
    p = build_image_payload("[主体]x+[场景]x+[风格]x+[光照]x+[构图]x+[质量]1K", "1K", "9:16")
    assert p["size"] == "1K" and p["extra_body"] == {"response_format": "url"}
    assert "response_format" not in p and "tags" not in p
    return True


# ---------- M1-Fix 返工用例（全 tmp_path 隔离，不写仓库 outputs） ----------

def _patch_outputs_to_tmp(monkeypatch, tmp_path):
    """把系列默认 outputs_root 重定向到 tmp_path（base_dir=None 时隔离）。"""
    import main as _m

    def _root(base_dir=None):
        if base_dir is not None:
            return Path(base_dir)
        return Path(tmp_path)

    monkeypatch.setattr(se, "_outputs_root", _root)
    try:
        monkeypatch.setattr(_m._M1_series, "_outputs_root", _root)
    except Exception:
        pass
    return _root


def _repo_outputs():
    return Path(__file__).resolve().parent.parent.parent / "outputs"


def test_fix_dot_traversal_rejected_tmp(tmp_path):
    for bad in (".", "..", "...", "....", "  ..  "):
        with pytest.raises(ValueError):
            se.series_dir(bad, base_dir=tmp_path)
        with pytest.raises(ValueError):
            se.create_series(bad, base_dir=tmp_path)
    # 正常名仍可用
    s = se.create_series("正常剧", base_dir=tmp_path)
    assert s["name"] == "正常剧"
    # 未污染仓库 outputs
    assert not (_repo_outputs() / "正常剧").exists()


def test_fix_legacy_single_collision_and_delete_keeps_old_tmp(tmp_path):
    # 旧单剧目录：有 script.json/state.json 但无 series.json
    legacy = tmp_path / "OldDrama"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "script.json").write_text("{}", encoding="utf-8")
    (legacy / "state.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="重名|旧单剧"):
        se.create_series("OldDrama", base_dir=tmp_path)
    # 新建系列后手动放老产物，delete 只删三件套、保留老产物
    se.create_series("KeepOld", base_dir=tmp_path)
    sdir = tmp_path / "KeepOld"
    (sdir / "script.json").write_text("{}", encoding="utf-8")
    (sdir / "state.json").write_text("{}", encoding="utf-8")
    assert se.delete_series("KeepOld", base_dir=tmp_path) is True
    assert not (sdir / "series.json").exists()
    assert not (sdir / "episodes").exists()
    assert not (sdir / "characters.json").exists()
    assert (sdir / "script.json").is_file()  # 老产物保留
    assert (sdir / "state.json").is_file()
    with pytest.raises(FileNotFoundError):
        se.get_series("KeepOld", base_dir=tmp_path)
    assert not (_repo_outputs() / "KeepOld").exists()
    assert not (_repo_outputs() / "OldDrama").exists()


def test_fix_planner_per12_total63_valid():
    durs = pl.clip_durations_for_total(63, per=12)
    assert abs(sum(float(d) for d in durs) - 63) < 1e-6
    for i, d in enumerate(durs):
        f = float(d)
        assert 4 <= f <= 12, (63, 12, durs)
        if i < len(durs) - 1:
            assert float(d).is_integer(), (63, 12, durs)
    # 全 per/total 扫一遍不断言越界（60-120s）
    for per in range(4, 13):
        for total in (60, 61, 62, 63, 64, 65, 71, 90, 120):
            ds = pl.clip_durations_for_total(total, per=per)
            assert abs(sum(float(d) for d in ds) - total) < 1e-6, (per, total, ds)
            for i, d in enumerate(ds):
                assert 4 <= float(d) <= 12, (per, total, ds)
                if i < len(ds) - 1:
                    assert float(d).is_integer(), (per, total, ds)


def test_fix_planner_dialogue_max_lower_bound():
    good = {
        "total_seconds": 60,
        "clip_durations": [8] * 7 + [4],
        "style_effective": "写实",
        "cast_plan": [],
        "market": {"hook_3s": "钩子", "beats": ["a", "b", "c", "d"],
                   "beats_per_15s": 1.0, "cliffhanger": "悬念", "risk": ""},
        "density": {"chars_per_sec": 10, "dialogue_max": 30},
    }
    for bad_max in (0, -1, -30):
        bad = {**good, "density": {"chars_per_sec": 10, "dialogue_max": bad_max}}
        with pytest.raises(ValueError):
            pl.coerce_plan(bad, 60)


def test_fix_characters_locked_string_and_voice_tmp(tmp_path):
    # locked="false" 字符串不得变 True；仅 True/1/true 为真
    assert ch.normalize_character(_char("A", id="c01", locked="false"))["locked"] is False
    assert ch.normalize_character(_char("A", id="c01", locked="False"))["locked"] is False
    assert ch.normalize_character(_char("A", id="c01", locked=""))["locked"] is False
    assert ch.normalize_character(_char("A", id="c01", locked=0))["locked"] is False
    assert ch.normalize_character(_char("A", id="c01", locked="true"))["locked"] is True
    assert ch.normalize_character(_char("A", id="c01", locked="True"))["locked"] is True
    assert ch.normalize_character(_char("A", id="c01", locked="1"))["locked"] is True
    assert ch.normalize_character(_char("A", id="c01", locked=1))["locked"] is True
    assert ch.normalize_character(_char("A", id="c01", locked=True))["locked"] is True
    # locked voice 不覆盖，但别名/立绘仍并入
    existing = [_char("阿雪", id="c01", locked=True,
                      voice={"provider": "edge-tts", "voice_id": "A"},
                      aliases=["雪姑娘"])]
    incoming = [_char("雪姑娘", id="c99",
                      voice={"provider": "edge-tts", "voice_id": "B"},
                      aliases=["小雪"], images=["characters/c99_x.png"])]
    merged = ch.merge_characters(existing, incoming)
    assert merged[0]["voice"]["voice_id"] == "A"
    assert merged[0]["voice"]["provider"] == "edge-tts"
    assert "characters/c99_x.png" in merged[0]["images"]
    assert "小雪" in merged[0]["aliases"]
    # 非锁定 voice 允许覆盖
    existing2 = [_char("阿牛", id="c01", locked=False,
                       voice={"provider": "", "voice_id": ""})]
    incoming2 = [_char("阿牛", voice={"provider": "edge-tts", "voice_id": "NEW"})]
    assert ch.merge_characters(existing2, incoming2)[0]["voice"]["voice_id"] == "NEW"
    # save/load 走 tmp_path，不污染仓库
    se.create_series("LockDrama", base_dir=tmp_path)
    ch.save_characters("LockDrama", existing, base_dir=tmp_path)
    assert ch.load_characters("LockDrama", base_dir=tmp_path)[0]["locked"] is True
    assert not (_repo_outputs() / "LockDrama").exists()


def test_fix_breakdown_rejects_illegal_plan_durations_tmp(tmp_path, monkeypatch):
    # 纯函数层：非法镜表直接拒绝，不透传
    with pytest.raises(ValueError):
        bd.build_breakdown_prompt("剧", 60, "9:16", "8", "写实", "原文" * 10,
                                  durations=[15] + [8] * 5 + [4])
    with pytest.raises(ValueError):
        bd.coerce_script({"clips": [{"narration": "x",
                                     "image_prompt": "[主体]x+[场景]x+[风格]x+[光照]x+[构图]x+[质量]1K",
                                     "video_prompt": "[主体]x+[动作]x+[场景]x+[运镜]x+[光照]x+[风格]x"}]},
                         "剧", 60, "9:16", "8", "写实", durations=[15, 45])
    # 路由层：plan 非法镜表 400 拒绝（tmp_path 隔离）
    _patch_outputs_to_tmp(monkeypatch, tmp_path)
    import main as _m
    from fastapi.testclient import TestClient

    _pool = _FakePool()
    monkeypatch.setattr(_m, "_pool", lambda: _pool)
    monkeypatch.setattr(_m, "_text_cfg", lambda: {
        "provider": "agnes", "model": "agnes-3.0-flash", "base_url": "",
        "temperature": 0.7, "max_tokens": 8192, "timeout_s": 30,
        "fallback": {"mode": "manual"}})
    c = TestClient(_m.app)
    name = "M1FIXBADDUR"
    assert c.post("/api/series/new", json={"name": name}).status_code == 200
    assert c.post(f"/api/series/{name}/episodes", json={
        "title": "E1", "total_seconds": 60, "source_text": "原文" * 200}).status_code == 200
    # 手写非法 plan（15 越界）
    se.write_episode_plan(name, "E01", {
        "total_seconds": 60, "clip_durations": [15, 45],
        "style_effective": "写实", "cast_plan": [],
        "market": {"hook_3s": "h", "beats": ["a", "b", "c", "d"],
                   "beats_per_15s": 1.0, "cliffhanger": "c", "risk": ""},
        "density": {"chars_per_sec": 10, "dialogue_max": 30}}, base_dir=tmp_path)
    r = c.post(f"/api/series/{name}/episodes/E01/breakdown", json={})
    assert r.status_code == 400, r.text
    assert "plan" in r.json().get("detail", "").lower() or "非法" in r.json().get("detail", "")
    assert not (_repo_outputs() / name).exists()


def test_fix_portrait_gen_kwargs_size_1k_no_tags_tmp(tmp_path, monkeypatch):
    # 路由 gen_fn kwargs 断言：size==1K 且无 tags/顶层 response_format
    _patch_outputs_to_tmp(monkeypatch, tmp_path)
    import main as _m
    from fastapi.testclient import TestClient

    pool = _FakePool(kind_ok=("text", "image"))
    monkeypatch.setattr(_m, "_pool", lambda: pool)
    monkeypatch.setattr(_m, "_text_cfg", lambda: {
        "provider": "agnes", "model": "agnes-3.0-flash", "base_url": "",
        "temperature": 0.7, "max_tokens": 8192, "timeout_s": 30,
        "fallback": {"mode": "manual"}})
    c = TestClient(_m.app)
    name = "M1FIXPORTRAIT"
    assert c.post("/api/series/new", json={"name": name}).status_code == 200
    ch.save_characters(name, [_char("阿雪", id="c01", appearance="短发少女")],
                       base_dir=tmp_path)
    seen: dict = {}
    tiny_png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfakepng").decode()

    def _cap_gen(prompt, **kw):
        seen.clear()
        seen.update(kw)
        seen["_prompt"] = prompt
        assert kw.get("size") == "1K"
        assert "tags" not in kw
        assert "response_format" not in kw
        return {"b64": tiny_png_b64, "generated_by": "agnes/agnes-image-2.5-flash@now"}

    monkeypatch.setattr(_m, "_generate_image", _cap_gen)
    try:
        monkeypatch.setattr(_m, "_M1_generate_image", _cap_gen)
    except AttributeError:
        pass
    r = c.post(f"/api/series/{name}/characters/c01/portrait", json={"style": "写实"})
    assert r.status_code == 200, r.text
    assert seen.get("size") == "1K"
    assert "tags" not in seen and "response_format" not in seen
    assert r.json()["path"].startswith("characters/")
    assert not (_repo_outputs() / name).exists()


def test_fix_multipart_missing_lib_400_tmp(tmp_path, monkeypatch):
    # 缺 python-multipart 时 multipart 直接 400 指引安装（不静默取首段）
    import asyncio

    import main as _m

    _patch_outputs_to_tmp(monkeypatch, tmp_path)
    name = "M1FIXMP"
    se.create_series(name, base_dir=tmp_path)

    class _FakeReq:
        headers = {"content-type": "multipart/form-data; boundary=----x"}
        query_params: dict = {}

        async def form(self):
            raise AssertionError(
                "Form data requires 'python-multipart' to be installed.")

        async def body(self):
            return b"------x\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.md\"\r\n\r\nhello\r\n------x--\r\n"

        async def json(self):
            return {}

    with pytest.raises(Exception) as exc:
        asyncio.run(_m.series_episode_create(name, _FakeReq()))  # type: ignore[arg-type]
    from fastapi import HTTPException as _HTTP

    assert isinstance(exc.value, _HTTP)
    assert exc.value.status_code == 400
    assert "python-multipart" in exc.value.detail
    assert not (_repo_outputs() / name).exists()


# ---------- 新建入口修复：drama_name 稳定键 + 老命名空间镜像 + 下限放宽 ----------

def _patch_all_roots_to_tmp(monkeypatch, tmp_path):
    """系列 + 老单剧命名空间全部重定向到 tmp（镜像不断言污染仓库 outputs）。"""
    import main as _m

    import orchestrator as _orch

    def _root(base_dir=None):
        if base_dir is not None:
            return Path(base_dir)
        return Path(tmp_path)

    monkeypatch.setattr(se, "_outputs_root", _root)
    try:
        monkeypatch.setattr(_m._M1_series, "_outputs_root", _root)
    except Exception:
        pass

    def _tmp_drama_dir(name, base_dir=None):
        if base_dir is not None:
            return Path(base_dir) / str(name)
        safe = "".join(c for c in str(name or "") if c not in '\\/:*?"<>|').strip()
        if not safe:
            raise ValueError("剧名不能为空")
        return Path(tmp_path) / safe

    monkeypatch.setattr(_orch, "_drama_dir", _tmp_drama_dir)
    try:
        monkeypatch.setattr(_m, "_drama_dir_fn", _tmp_drama_dir)
    except Exception:
        pass
    return _root


def test_episode_drama_name_stable_on_rename_tmp(tmp_path):
    se.create_series("稳键剧", base_dir=tmp_path)
    e1 = se.create_episode("稳键剧", title="第一集", source_text="原文" * 10,
                           total_seconds=30, base_dir=tmp_path)
    assert e1["drama_name"] == "稳键剧 E01"
    # 改标题不改稳定键
    patched = se.update_episode("稳键剧", "E01", {"title": "新标题"}, base_dir=tmp_path)
    assert patched["drama_name"] == "稳键剧 E01"
    got = se.get_series("稳键剧", base_dir=tmp_path)
    assert got["episodes"][0]["drama_name"] == "稳键剧 E01"
    # 老数据（无 drama_name）读取时回填
    import json as _json

    mpath = tmp_path / "稳键剧" / "episodes" / "E01" / "meta.json"
    meta = _json.loads(mpath.read_text(encoding="utf-8"))
    del meta["drama_name"]
    mpath.write_text(_json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    assert se.get_episode("稳键剧", "E01", base_dir=tmp_path)["drama_name"] == "稳键剧 E01"
    assert not (_repo_outputs() / "稳键剧").exists()


def test_episode_bounds_relaxed_and_mirror_tmp(tmp_path, monkeypatch):
    import json as _json
    from urllib.parse import quote

    _patch_all_roots_to_tmp(monkeypatch, tmp_path)
    import main as _m
    from fastapi.testclient import TestClient

    pool = _FakePool(kind_ok=("text", "image"))
    monkeypatch.setattr(_m, "_pool", lambda: pool)
    monkeypatch.setattr(_m, "_text_cfg", lambda: {
        "provider": "agnes", "model": "agnes-3.0-flash", "base_url": "",
        "temperature": 0.7, "max_tokens": 8192, "timeout_s": 30,
        "fallback": {"mode": "manual"}})
    c = TestClient(_m.app)
    name = "TMPMIR"
    assert c.post("/api/series/new", json={"name": name}).status_code == 200
    # 下限放宽：30s 可建，0/负数拒绝
    r = c.post(f"/api/series/{name}/episodes", json={
        "title": "短集", "total_seconds": 30, "source_text": "原文" * 100})
    assert r.status_code == 200, r.text
    assert r.json()["episode"]["drama_name"] == f"{name} E01"
    r = c.post(f"/api/series/{name}/episodes", json={
        "title": "零", "total_seconds": 0, "source_text": "原文"})
    assert r.status_code == 400
    r = c.patch(f"/api/series/{name}/episodes/E01", json={"total_seconds": 45})
    assert r.status_code == 200, r.text
    r = c.patch(f"/api/series/{name}/episodes/E01", json={"total_seconds": 0})
    assert r.status_code == 400
    # plan 下限放宽：45s 可规划（beats>=ceil(45/15)=3）
    plan30 = {"total_seconds": 45, "clip_durations": [8, 8, 8, 8, 7, 6],
              "style_effective": "写实", "cast_plan": [],
              "market": {"hook_3s": "钩", "beats": ["a", "b", "c"],
                         "beats_per_15s": 1.0, "cliffhanger": "悬", "risk": ""},
              "density": {"chars_per_sec": 10, "dialogue_max": 30}}

    def _fake_text(prompt, **kw):
        if "策划" in prompt or "clip_durations" in prompt:
            return {"text": _json.dumps(plan30, ensure_ascii=False),
                    "generated_by": "fake/plan@now"}
        clips = _canned_script_clips(n=6, durs=[8, 8, 8, 8, 7, 6])
        return {"text": _json.dumps(
            {"title": "短集", "total_seconds": 45, "aspect": "9:16",
             "resolution": "720x1280", "clip_seconds": "8",
             "character_refs": [], "clips": clips}, ensure_ascii=False),
            "generated_by": "fake/bd@now"}

    monkeypatch.setattr(_m, "_generate_text", _fake_text)
    r = c.post(f"/api/series/{name}/plan", json={"episode_id": "E01", "style": "写实"})
    assert r.status_code == 200, r.text
    # breakdown 落盘 + 镜像到老命名空间
    r = c.post(f"/api/series/{name}/episodes/E01/breakdown", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["drama_name"] == f"{name} E01"
    legacy = tmp_path / f"{name} E01"
    assert (legacy / "script.json").is_file()
    assert (legacy / "state.json").is_file()
    # 老通道可读：分镜表 ?name= / 队列 / SSE 全部复用
    key = quote(f"{name} E01", safe="")
    r = c.get(f"/api/drama/{key}/state")
    assert r.status_code == 200, r.text
    assert r.json()["script"]["total_seconds"] == pytest.approx(45.0)
    assert not (_repo_outputs() / name).exists()
    assert not (_repo_outputs() / f"{name} E01").exists()


# ---------- B1 后端契约缺口补齐（前端 7 路由 + style_override + 角色映射 + 文件路由） ----------

def _b1_client(monkeypatch, tmp_path):
    """B1 契约测试客户端：全部 roots 重定向 tmp + fake 文本配置（文本体由各用例再 mock）。"""
    from fastapi.testclient import TestClient

    import main as _m

    _patch_all_roots_to_tmp(monkeypatch, tmp_path)
    pool = _FakePool(kind_ok=("text", "image"))
    monkeypatch.setattr(_m, "_pool", lambda: pool)
    monkeypatch.setattr(_m, "_text_cfg", lambda: {
        "provider": "agnes", "model": "agnes-3.0-flash", "base_url": "",
        "temperature": 0.7, "max_tokens": 8192, "timeout_s": 30,
        "fallback": {"mode": "manual"}})
    return TestClient(_m.app), _m, pool


def test_b1_post_series_alias_and_episodes_list_tmp(monkeypatch, tmp_path):
    c, m, pool = _b1_client(monkeypatch, tmp_path)
    # POST /api/series{title} → 200（前端实际形状，无 name 键）
    r = c.post("/api/series", json={"title": "B1ALIAS", "style": "写实"})
    assert r.status_code == 200, r.text
    assert r.json()["series"]["name"] == "B1ALIAS"
    # name/title 双空 → 400
    r = c.post("/api/series", json={"title": "   "})
    assert r.status_code == 400
    # 建集带风格 → meta 回显
    r = c.post("/api/series/B1ALIAS/episodes", json={
        "title": "首集", "total_seconds": 60,
        "source_text": "原文" * 200, "style_override": "赛博朋克"})
    assert r.status_code == 200, r.text
    assert r.json()["episode"]["style_override"] == "赛博朋克"
    # PATCH 分集风格
    r = c.patch("/api/series/B1ALIAS/episodes/E01", json={"style_override": "新风格"})
    assert r.status_code == 200, r.text
    assert r.json()["episode"]["style_override"] == "新风格"
    # GET .../episodes：七键齐全
    r = c.get("/api/series/B1ALIAS/episodes")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and len(body["episodes"]) == 1
    item = body["episodes"][0]
    for k in ("id", "title", "drama_name", "total_seconds",
              "style_override", "word_count", "status"):
        assert k in item, (k, item)
    assert item["drama_name"] == "B1ALIAS E01"
    assert item["style_override"] == "新风格"
    assert item["word_count"] == len("原文" * 200)
    assert item["status"] in ("draft", "planned", "scripted")
    assert not (_repo_outputs() / "B1ALIAS").exists()


def test_b1_upload_and_episode_plan_tmp(monkeypatch, tmp_path):
    import json as _json

    c, m, pool = _b1_client(monkeypatch, tmp_path)
    assert c.post("/api/series", json={"title": "B1UP"}).status_code == 200
    # upload 路由 JSON 分支（不依赖 python-multipart，常跑）
    r = c.post("/api/series/B1UP/episodes/upload", json={
        "title": "JSON集", "total_seconds": 60,
        "source_text": "正文" * 100, "style_override": "水墨"})
    assert r.status_code == 200, r.text
    assert r.json()["episode"]["style_override"] == "水墨"
    # multipart upload（缺 python-multipart 则跳过）
    try:
        import multipart  # noqa: F401
        has_mp = True
    except ImportError:
        try:
            import python_multipart  # noqa: F401
            has_mp = True
        except ImportError:
            has_mp = False
    if not has_mp:
        pytest.skip("python-multipart 缺失，跳过 multipart 上传断言")
    r = c.post("/api/series/B1UP/episodes/upload",
               files={"file": ("chap.md", ("上传正文" * 100).encode("utf-8"), "text/markdown")},
               data={"title": "文件集", "style_override": "水墨", "total_seconds": "60"})
    assert r.status_code == 200, r.text
    ep = r.json()["episode"]
    assert ep["id"] == "E02"  # U1 修：JSON 分支已建 E01，multipart 第二集为 E02（有库才跑到此，原 E01 恒败）
    assert ep["style_override"] == "水墨"
    # POST .../episodes/{ep}/plan{} → 200 且 hook 非空（fake 文本）
    def _fake_text(prompt, **kw):
        return {"text": _json.dumps(_canned_plan(), ensure_ascii=False),
                "generated_by": "fake/plan@now"}

    monkeypatch.setattr(m, "_generate_text", _fake_text)
    r = c.post("/api/series/B1UP/episodes/E01/plan", json={})
    assert r.status_code == 200, r.text
    assert r.json()["plan"]["market"]["hook_3s"] == "钩子"
    assert not (_repo_outputs() / "B1UP").exists()


def test_b1_characters_crud_merge_tmp(monkeypatch, tmp_path):
    c, m, pool = _b1_client(monkeypatch, tmp_path)
    assert c.post("/api/series", json={"title": "B1CH"}).status_code == 200
    # POST .../characters{desc} → desc==logline
    r = c.post("/api/series/B1CH/characters", json={"name": "阿雪", "desc": "白衣少年小传"})
    assert r.status_code == 200, r.text
    ch1 = r.json()["character"]
    assert ch1["desc"] == "白衣少年小传" == ch1["logline"]
    assert ch1["id"].startswith("c")
    assert ch1["portrait_url"] == "" and ch1["is_main"] is False
    r = c.post("/api/series/B1CH/characters", json={"name": "阿牛", "desc": "憨厚"})
    assert r.status_code == 200, r.text
    id1, id2 = ch1["id"], r.json()["character"]["id"]
    assert id1 != id2
    # PATCH is_main 单主图语义
    r = c.patch(f"/api/series/B1CH/characters/{id1}", json={"is_main": True})
    assert r.status_code == 200 and r.json()["character"]["is_main"] is True
    r = c.patch(f"/api/series/B1CH/characters/{id2}", json={"is_main": True})
    assert r.status_code == 200 and r.json()["character"]["is_main"] is True
    got = {x["id"]: x for x in c.get("/api/series/B1CH/characters").json()["characters"]}
    assert got[id1]["is_main"] is False and got[id2]["is_main"] is True
    # PATCH desc 写入 logline
    r = c.patch(f"/api/series/B1CH/characters/{id1}", json={"desc": "新小传"})
    assert r.status_code == 200, r.text
    assert r.json()["character"]["logline"] == "新小传"
    assert r.json()["character"]["desc"] == "新小传"
    # merge 折叠：来源别名/立绘并入 target，target 为空取首个非空 source
    assert c.patch(f"/api/series/B1CH/characters/{id1}",
                   json={"aliases": ["雪姑娘"], "images": ["characters/a.png"]}).status_code == 200
    assert c.patch(f"/api/series/B1CH/characters/{id2}", json={"logline": ""}).status_code == 200
    r = c.post("/api/series/B1CH/characters/merge",
               json={"source_ids": [id1], "target_id": id2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["target_id"] == id2 and body["deleted"] == [id1]
    assert "雪姑娘" in body["character"]["aliases"]
    assert "characters/a.png" in body["character"]["images"]
    assert body["character"]["logline"] == "新小传"
    rest = c.get("/api/series/B1CH/characters").json()
    assert rest["count"] == 1 and rest["characters"][0]["id"] == id2
    # merge 错误形：空来源 / 目标不存在 → 400
    assert c.post("/api/series/B1CH/characters/merge",
                  json={"source_ids": [], "target_id": id2}).status_code == 400
    assert c.post("/api/series/B1CH/characters/merge",
                  json={"source_ids": [id2], "target_id": "c99"}).status_code == 400
    # DELETE → 404 gone
    r = c.delete(f"/api/series/B1CH/characters/{id2}")
    assert r.status_code == 200 and r.json()["deleted"] == id2
    assert c.patch(f"/api/series/B1CH/characters/{id2}", json={"desc": "x"}).status_code == 404
    assert c.delete(f"/api/series/B1CH/characters/{id2}").status_code == 404
    assert not (_repo_outputs() / "B1CH").exists()


def test_b1_files_and_portrait_tmp(monkeypatch, tmp_path):
    c, m, pool = _b1_client(monkeypatch, tmp_path)
    assert c.post("/api/series", json={"title": "B1FILE"}).status_code == 200
    r = c.post("/api/series/B1FILE/characters", json={"name": "阿雪", "desc": "立绘模特"})
    cid = r.json()["character"]["id"]
    tiny_png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfakepng").decode()
    monkeypatch.setattr(m, "_generate_image",
                        lambda prompt, **kw: {"b64": tiny_png_b64,
                                             "generated_by": "agnes/agnes-image-2.5-flash@now"})
    try:
        monkeypatch.setattr(m, "_M1_generate_image",
                            lambda prompt, **kw: {"b64": tiny_png_b64,
                                                 "generated_by": "agnes/agnes-image-2.5-flash@now"})
    except AttributeError:
        pass
    r = c.post(f"/api/series/B1FILE/characters/{cid}/portrait", json={"style": "写实"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"].startswith("characters/")
    assert body["portrait_url"].startswith("/api/")
    assert body["character"]["portrait_url"].startswith("/api/")
    assert "files?path=" in body["portrait_url"]
    # 正常 200（media_type png）
    r = c.get("/api/series/B1FILE/files", params={"path": body["path"]})
    assert r.status_code == 200, r.text
    assert r.headers.get("content-type", "").startswith("image/png")
    # 越界 / 不存在 / 非法后缀 → 404
    assert c.get("/api/series/B1FILE/files", params={"path": "../series.json"}).status_code == 404
    assert c.get("/api/series/B1FILE/files", params={"path": "../../x.png"}).status_code == 404
    assert c.get("/api/series/B1FILE/files", params={"path": "characters/nope.png"}).status_code == 404
    assert c.get("/api/series/B1FILE/files",
                 params={"path": "episodes/E01/meta.json"}).status_code == 404
    assert not (_repo_outputs() / "B1FILE").exists()


def test_b1_style_override_e2e_tmp(monkeypatch, tmp_path):
    import json as _json

    c, m, pool = _b1_client(monkeypatch, tmp_path)
    assert c.post("/api/series", json={"title": "B1STYLE", "style": "系列风"}).status_code == 200
    r = c.post("/api/series/B1STYLE/episodes", json={
        "title": "风格集", "total_seconds": 60,
        "source_text": "少年雪夜下山" * 200, "style_override": "赛博朋克风"})
    assert r.status_code == 200, r.text
    # plan：req 空 → ep.style_override 进题面
    seen: dict = {}

    def _fake_plan(prompt, **kw):
        seen["plan_prompt"] = prompt
        return {"text": _json.dumps(_canned_plan(), ensure_ascii=False),
                "generated_by": "fake/plan@now"}

    monkeypatch.setattr(m, "_generate_text", _fake_plan)
    r = c.post("/api/series/B1STYLE/plan", json={"episode_id": "E01"})
    assert r.status_code == 200, r.text
    assert "赛博朋克风" in seen.get("plan_prompt", "")
    # breakdown：建集风格 → 分镜 fake 捕获 prompt 含该风格词
    # （E01 已有 plan 且 plan.style_effective 优先，故另建无 plan 的 E02）
    r = c.post("/api/series/B1STYLE/episodes", json={
        "title": "风格集二", "total_seconds": 60,
        "source_text": "少年雪夜下山" * 200, "style_override": "赛博朋克风"})
    assert r.status_code == 200, r.text

    def _fake_bd(prompt, **kw):
        seen["bd_prompt"] = prompt
        clips = _canned_script_clips()
        return {"text": _json.dumps(
            {"title": "风格集二", "total_seconds": 60, "aspect": "9:16",
             "resolution": "720x1280", "clip_seconds": "8",
             "character_refs": [], "clips": clips}, ensure_ascii=False),
            "generated_by": "fake/bd@now"}

    monkeypatch.setattr(m, "_generate_text", _fake_bd)
    r = c.post("/api/series/B1STYLE/episodes/E02/breakdown", json={})
    assert r.status_code == 200, r.text
    assert "赛博朋克风" in seen.get("bd_prompt", "")
    # req.style 显式覆盖建集风格
    seen.clear()
    r = c.post("/api/series/B1STYLE/episodes/E02/breakdown", json={"style": "水墨风"})
    assert r.status_code == 200, r.text
    assert "水墨风" in seen.get("bd_prompt", "")
    assert not (_repo_outputs() / "B1STYLE").exists()
    assert not (_repo_outputs() / "B1STYLE E01").exists()


# ---------- C1 AI 定时长（planner None + plan 回写 + breakdown 自动规划） ----------

def _canned_ai_plan_total(total=64):
    import math as _math
    # 去锚：假数据须多样化（镜数≥3 含至少两种秒数），不可用全等凑整
    if int(total) == 64:
        durs = [6, 7, 8, 9, 8, 7, 9, 10]
        assert sum(durs) == 64
    else:
        import planner as _pl
        durs = _pl.clip_durations_for_total(total)
        if len(durs) >= 3 and len({float(d) for d in durs}) == 1:
            durs = list(durs)
            durs[0] = int(durs[0]) - 1
            durs[1] = int(durs[1]) + 1
            assert sum(float(d) for d in durs) == float(total)
    need = max(1, _math.ceil(float(total) / 15))
    return {"total_seconds": float(total), "clip_durations": durs,
            "style_effective": "写实",
            "cast_plan": [],
            "market": {"hook_3s": "钩子", "beats": [f"b{i}" for i in range(need)],
                       "beats_per_15s": 1.0, "cliffhanger": "悬念", "risk": ""},
            "density": {"chars_per_sec": 10, "dialogue_max": 30}}


def test_c1_planner_none_total_prompt_and_coerce():
    # prompt(None)：密度 8-14、≥4s、4-12、ceil(total/15)动态、hook/悬念/45 齐全
    system, user = pl.build_planner_prompt("剧", None, "写实", "原文" * 50)
    assert "8-14" in user and "4s" in user
    assert "4-12" in user and "ceil(total/15)" in user
    assert "hook_3s" in user or "钩子" in user
    assert "15s" in user and "悬念" in user
    assert "45" in user
    # coerce(None)：取 AI 提议值并校验求和
    raw = _canned_ai_plan_total(64)
    plan = pl.coerce_plan(raw, None, "写实")
    assert plan["total_seconds"] == pytest.approx(64.0)
    assert abs(sum(float(d) for d in plan["clip_durations"]) - 64) < 1e-6
    assert pl.validate_plan(plan) is True
    # 求和不一致拒绝；缺 total 拒绝；<4s 拒绝
    bad_sum = dict(raw, clip_durations=[8] * 7 + [5])
    with pytest.raises(ValueError):
        pl.coerce_plan(bad_sum, None)
    bad_no_total = {k: v for k, v in raw.items() if k != "total_seconds"}
    with pytest.raises(ValueError):
        pl.coerce_plan(bad_no_total, None)
    bad_small = _canned_ai_plan_total(64)
    bad_small["total_seconds"] = 3
    bad_small["clip_durations"] = [3]
    with pytest.raises(ValueError):
        pl.coerce_plan(bad_small, None)
    # 显式 total 行为不变
    good60 = _canned_plan()
    assert pl.coerce_plan(good60, 60, "写实")["total_seconds"] == 60
    assert not (_repo_outputs() / "C1UNIT").exists()


def test_c1_plan_ai_total_and_writeback_tmp(monkeypatch, tmp_path):
    import json as _json

    c, m, pool = _b1_client(monkeypatch, tmp_path)
    name = "C1AIPLAN"
    assert c.post("/api/series", json={"title": name}).status_code == 200
    r = c.post(f"/api/series/{name}/episodes", json={
        "title": "首集", "total_seconds": 60,
        "source_text": "少年雪夜下山" * 200})
    assert r.status_code == 200, r.text
    ai_plan = _canned_ai_plan_total(64)
    # grounding 端到端：原文真名随 plan 落盘（库为空跳过库检）
    ai_plan["cast_plan"] = [{"name": "少年", "role": "主角", "episodes": ["E01"]}]

    def _fake_ai(prompt, **kw):
        return {"text": _json.dumps(ai_plan, ensure_ascii=False),
                "generated_by": "fake/ai-plan@now"}

    monkeypatch.setattr(m, "_generate_text", _fake_ai)
    # 不带 total → AI 定时长
    r = c.post(f"/api/series/{name}/plan", json={"episode_id": "E01"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_source"] == "ai"
    assert body["plan"]["total_seconds"] == pytest.approx(64.0)
    assert body["plan"]["total_seconds"] >= 4
    # 分集 total 被回写（触发系列总量重算）
    r = c.get(f"/api/series/{name}/episodes/E01")
    assert r.status_code == 200, r.text
    assert float(r.json()["episode"]["total_seconds"]) == pytest.approx(64.0)
    r = c.get(f"/api/series/{name}")
    assert r.status_code == 200, r.text
    assert float(r.json()["series"]["total_seconds"]) == pytest.approx(64.0)
    # plan.json 落盘
    assert (tmp_path / name / "episodes" / "E01" / "plan.json").is_file()
    assert not (_repo_outputs() / name).exists()
    import json as _json2
    saved = _json2.loads((tmp_path / name / "episodes" / "E01" / "plan.json").read_text(encoding="utf-8"))
    assert [e["name"] for e in saved["cast_plan"]] == ["少年"]


def test_c1_plan_explicit_total_manual_tmp(monkeypatch, tmp_path):
    import json as _json

    c, m, pool = _b1_client(monkeypatch, tmp_path)
    name = "C1MANUAL"
    assert c.post("/api/series", json={"title": name}).status_code == 200
    assert c.post(f"/api/series/{name}/episodes", json={
        "title": "首集", "total_seconds": 60,
        "source_text": "少年雪夜下山" * 200}).status_code == 200

    def _fake_manual(prompt, **kw):
        return {"text": _json.dumps(_canned_plan(), ensure_ascii=False),
                "generated_by": "fake/plan@now"}

    monkeypatch.setattr(m, "_generate_text", _fake_manual)
    r = c.post(f"/api/series/{name}/plan",
               json={"episode_id": "E01", "total_seconds": 60})
    assert r.status_code == 200, r.text
    assert r.json()["total_source"] == "manual"
    assert r.json()["plan"]["total_seconds"] == pytest.approx(60.0)
    # per-ep 薄封装显式 total 同为 manual
    r = c.post(f"/api/series/{name}/episodes/E01/plan",
               json={"total_seconds": 60})
    assert r.status_code == 200, r.text
    assert r.json()["total_source"] == "manual"
    # per-ep 省略 total → ai（与 /plan 一致）
    r = c.post(f"/api/series/{name}/episodes/E01/plan", json={})
    assert r.status_code == 200, r.text
    assert r.json()["total_source"] == "ai"
    assert not (_repo_outputs() / name).exists()


def test_c1_breakdown_auto_plan_tmp(monkeypatch, tmp_path):
    import json as _json

    c, m, pool = _b1_client(monkeypatch, tmp_path)
    name = "C1AUTOBD"
    assert c.post("/api/series", json={"title": name}).status_code == 200
    assert c.post(f"/api/series/{name}/episodes", json={
        "title": "首集", "total_seconds": 60,
        "source_text": "少年雪夜下山" * 200}).status_code == 200
    # 第二集用于显式均匀切分对照（无 plan + 显式 8 → 不自动规划）
    assert c.post(f"/api/series/{name}/episodes", json={
        "title": "二集", "total_seconds": 60,
        "source_text": "少女晨雾登山" * 200}).status_code == 200

    def _fake_both(prompt, **kw):
        if "clip_durations" in (prompt or ""):
            return {"text": _json.dumps(_canned_plan(), ensure_ascii=False),
                    "generated_by": "fake/plan@now"}
        clips = _canned_script_clips()
        return {"text": _json.dumps(
            {"title": "首集", "total_seconds": 60, "aspect": "9:16",
             "resolution": "720x1280", "clip_seconds": "8",
             "character_refs": [], "clips": clips}, ensure_ascii=False),
            "generated_by": "fake/bd@now"}

    monkeypatch.setattr(m, "_generate_text", _fake_both)
    # E01 无 plan + clip_seconds=None → 自动规划后分镜
    assert not (tmp_path / name / "episodes" / "E01" / "plan.json").exists()
    r = c.post(f"/api/series/{name}/episodes/E01/breakdown",
               json={"clip_seconds": None})
    assert r.status_code == 200, r.text
    assert (tmp_path / name / "episodes" / "E01" / "plan.json").is_file()
    assert len(r.json()["script"]["clips"]) == 8
    # "" 同为自动
    r = c.post(f"/api/series/{name}/episodes/E01/breakdown",
               json={"clip_seconds": ""})
    assert r.status_code == 200, r.text
    # E02 显式 8 + 无 plan → 均匀切分，不自动写 plan.json
    assert not (tmp_path / name / "episodes" / "E02" / "plan.json").exists()
    r = c.post(f"/api/series/{name}/episodes/E02/breakdown",
               json={"clip_seconds": "8"})
    assert r.status_code == 200, r.text
    assert not (tmp_path / name / "episodes" / "E02" / "plan.json").exists()
    assert not (_repo_outputs() / name).exists()
    import shutil as _sh
    _sh.rmtree(tmp_path / f"{name} E01", ignore_errors=True)
    _sh.rmtree(tmp_path / f"{name} E02", ignore_errors=True)


# ---------- E1 角色链路（去上限/占位回退/渲染接入；全 tmp 隔离） ----------

def _e1_char(name, i, **kw):
    base = {"name": name, "aliases": [], "logline": f"{name}小传",
            "appearance": f"{name}短发劲装", "personality": "冷静",
            "relation": "主角阵营", "outfit": "劲装",
            "status": "待确认", "images": [], "voice": {},
            "first_seen": "E01", "notes": "", "locked": False,
            "is_main": False}
    base.update(kw)
    return base


def test_e1_coerce_no_cap_and_skipped_tmp(tmp_path):
    # 7 角色全收（不限 5）；单条坏数据跳过计数，全坏才 502
    raw = {"characters": [_e1_char(f"角色{i:02d}", i) for i in range(7)]}
    out, skipped = ch.coerce_characters_with_skipped(raw, first_seen="E01")
    assert len(out) == 7 and skipped == 0
    assert [c["id"] for c in out] == [f"c{i + 1:02d}" for i in range(7)]
    # 兼容旧签名仍返列表
    assert len(ch.coerce_characters(raw, first_seen="E01")) == 7
    # 1 好 + 1 超长 appearance + 1 非 dict → 跳过 2 条
    bad = {"characters": [
        _e1_char("好人", 0),
        _e1_char("坏人", 1, appearance="字" * 201),
        "notadict",
        {"aliases": []},  # 缺名
    ]}
    out2, skipped2 = ch.coerce_characters_with_skipped(bad)
    assert len(out2) == 1 and skipped2 == 3
    # 全坏才 502
    with pytest.raises(ValueError):
        ch.coerce_characters_with_skipped({"characters": [{"no": "name"}]})
    with pytest.raises(ValueError):
        ch.coerce_characters_with_skipped({})
    # 新字段归一/校验/透出默认值
    c0 = ch.normalize_character({"id": "c01", "name": "阿雪"})
    assert c0["status"] == "待确认" and c0["relation"] == "" and c0["outfit"] == ""
    assert ch.validate_character({**c0, "status": "已确认"}) is True
    with pytest.raises(ValueError):
        ch.validate_character({**c0, "status": "未知"})
    assert not (_repo_outputs() / "E1UNIT").exists()


def test_e1_merge_new_fields_and_status_sticky():
    a = _e1_char("阿雪", 0)
    a.update({"id": "c01", "relation": "", "outfit": "", "status": "已确认"})
    b = _e1_char("阿雪", 1)
    b.update({"relation": "师门", "outfit": "白衣", "status": "待确认"})
    merged = ch.merge_characters([a], [b])
    assert merged[0]["relation"] == "师门" and merged[0]["outfit"] == "白衣"
    assert merged[0]["status"] == "已确认"  # 已确认不被待确认回退
    # locked 一律不覆盖新字段
    a2 = _e1_char("阿牛", 0)
    a2.update({"id": "c01", "locked": True, "relation": "旧"})
    b2 = _e1_char("阿牛", 1)
    b2.update({"relation": "新", "outfit": "新衣", "status": "已确认"})
    m2 = ch.merge_characters([a2], [b2])
    assert m2[0]["relation"] == "旧" and m2[0]["outfit"] == "劲装"  # 锁定：保持原值


def test_e1_extract_count_skipped_tmp(monkeypatch, tmp_path):
    import json as _json

    c, m, pool = _b1_client(monkeypatch, tmp_path)
    name = "E1EXTRACT"
    assert c.post("/api/series", json={"title": name}).status_code == 200
    assert c.post(f"/api/series/{name}/episodes", json={
        "title": "首集", "total_seconds": 60,
        "source_text": "少年雪夜下山阿雪" * 200}).status_code == 200

    def _fake(prompt, **kw):
        return {"text": _json.dumps({"characters": [
            {"name": "阿雪", "appearance": "短发少女", "relation": "师门",
             "outfit": "白衣", "status": "待确认"},
            {"name": "坏条", "appearance": "字" * 201},
            "notadict",
        ]}, ensure_ascii=False), "generated_by": "fake/ch@now"}

    monkeypatch.setattr(m, "_generate_text", _fake)
    r = c.post(f"/api/series/{name}/characters/extract", json={"episode_id": "E01"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skipped"] == 2
    assert body["count"] == 1
    shown = {x["name"]: x for x in body["characters"]}
    assert shown["阿雪"]["relation"] == "师门"
    assert shown["阿雪"]["outfit"] == "白衣"
    assert shown["阿雪"]["status"] == "待确认"
    assert shown["阿雪"]["first_seen"] == "E01"
    assert not (_repo_outputs() / name).exists()


def test_e1_extract_cast_plan_placeholder_tmp(monkeypatch, tmp_path):
    import json as _json

    c, m, pool = _b1_client(monkeypatch, tmp_path)
    name = "E1PLACEHOLDER"
    assert c.post("/api/series", json={"title": name}).status_code == 200
    assert c.post(f"/api/series/{name}/episodes", json={
        "title": "首集", "total_seconds": 60,
        "source_text": "少年雪夜下山阿雪阿牛" * 200}).status_code == 200
    plan = _canned_plan()
    plan["cast_plan"] = [{"name": "阿雪", "role": "主角"},
                         {"name": "阿牛", "role": "配角"}]
    se.write_episode_plan(name, "E01", plan, base_dir=tmp_path)
    monkeypatch.setattr(m, "_generate_text",
                        lambda prompt, **kw: {"text": "根本不是 JSON{{{",
                                             "generated_by": "fake/bad@now"})
    r = c.post(f"/api/series/{name}/characters/extract", json={"episode_id": "E01"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "占位" in body.get("note", "")
    assert body["skipped"] == 0 and body["count"] == 2
    shown = {x["name"]: x for x in body["characters"]}
    assert set(shown) == {"阿雪", "阿牛"}
    for x in shown.values():
        assert x["status"] == "待确认"
        assert x["first_seen"] == "E01"
    # 两者皆无（删 plan + LLM 无效）才 502
    (tmp_path / name / "episodes" / "E01" / "plan.json").unlink()
    r = c.post(f"/api/series/{name}/characters/extract", json={"episode_id": "E01"})
    assert r.status_code == 502, r.text
    assert not (_repo_outputs() / name).exists()


def test_e1_extract_seven_no_cap_tmp(monkeypatch, tmp_path):
    import json as _json

    c, m, pool = _b1_client(monkeypatch, tmp_path)
    name = "E1SEVEN"
    assert c.post("/api/series", json={"title": name}).status_code == 200
    assert c.post(f"/api/series/{name}/episodes", json={
        "title": "首集", "total_seconds": 60,
        "source_text": "群像戏角色00角色01角色02角色03角色04角色05角色06" * 40}).status_code == 200

    def _fake(prompt, **kw):
        return {"text": _json.dumps({"characters": [
            {"name": f"角色{i:02d}", "appearance": "短发"} for i in range(7)]},
            ensure_ascii=False), "generated_by": "fake/ch7@now"}

    monkeypatch.setattr(m, "_generate_text", _fake)
    r = c.post(f"/api/series/{name}/characters/extract", json={"episode_id": "E01"})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 7 and r.json()["skipped"] == 0
    assert not (_repo_outputs() / name).exists()


def _e1_canned_clips_with_cast():
    durs = [8] * 7 + [4]
    clips = []
    cursor = 0.0
    for i, d in enumerate(durs):
        clips.append({"id": f"s{i + 1:02d}", "start": cursor, "duration": d,
                      "narration": f"第{i + 1}镜旁白",
                      "cast": ["阿雪"] if i % 2 == 0 else ["阿雪", "阿牛"],
                      "image_prompt": "[主体]少年+[场景]雪夜+[风格]写实+[光照]月光+[构图]竖构图+[质量]1K,高细节",
                      "video_prompt": "[主体]少年+[动作]抬头+[场景]雪夜+[运镜]缓慢推镜+[光照]月光+[风格]写实",
                      "video_mode": "text"})
        cursor += d
    return clips


def test_e1_breakdown_cast_and_refs_tmp(monkeypatch, tmp_path):
    import json as _json

    c, m, pool = _b1_client(monkeypatch, tmp_path)
    name = "E1BDCAST"
    assert c.post("/api/series", json={"title": name}).status_code == 200
    assert c.post(f"/api/series/{name}/episodes", json={
        "title": "首集", "total_seconds": 60,
        "source_text": "少年雪夜下山" * 200}).status_code == 200
    plan = _canned_plan()
    plan["cast_plan"] = [{"name": "阿雪", "role": "主角"},
                         {"name": "阿牛", "role": "配角"}]
    se.write_episode_plan(name, "E01", plan, base_dir=tmp_path)
    lib = [_e1_char("阿雪", 0),
           _e1_char("阿牛", 1)]
    lib[0].update({"id": "c01", "is_main": True,
                   "appearance": "白衣短发少年眸如寒星",
                   "images": ["characters/c01_main.png"]})
    lib[1].update({"id": "c02", "appearance": "憨厚壮汉络腮胡",
                   "images": ["characters/c02_main.png"]})
    ch.save_characters(name, [ch.normalize_character(x) for x in lib],
                       base_dir=tmp_path)
    seen: dict = {}

    def _fake(prompt, **kw):
        seen["prompt"] = prompt
        return {"text": _json.dumps(
            {"title": "首集", "total_seconds": 60, "aspect": "9:16",
             "resolution": "720x1280", "clip_seconds": "8",
             "character_refs": [], "clips": _e1_canned_clips_with_cast()},
            ensure_ascii=False), "generated_by": "fake/bd@now"}

    monkeypatch.setattr(m, "_generate_text", _fake)
    r = c.post(f"/api/series/{name}/episodes/E01/breakdown", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    script = body["script"]
    # prompt 按 cast_plan 要求写 cast[]
    assert "cast" in seen.get("prompt", "")
    assert "阿雪" in seen.get("prompt", "")
    # 每镜 cast 透传
    assert len(script["clips"]) == 8
    for i, clip in enumerate(script["clips"]):
        assert isinstance(clip.get("cast"), list), clip
        assert clip["cast"] == (["阿雪"] if i % 2 == 0 else ["阿雪", "阿牛"])
    # character_refs 非空 ≤5，本集出场 + is_main 居前取主图
    refs = script["character_refs"]
    assert 1 <= len(refs) <= 5
    assert refs[0] == "characters/c01_main.png"
    assert "characters/c02_main.png" in refs
    # image_prompt[主体]注入外貌短描，六段式齐全
    first = script["clips"][0]
    assert "出场" in first["image_prompt"] and "白衣短发少年" in first["image_prompt"]
    for seg in ("[主体]", "[场景]", "[风格]", "[光照]", "[构图]", "[质量]"):
        assert seg in first["image_prompt"]
    duo = script["clips"][1]
    assert "白衣短发少年" in duo["image_prompt"] and "憨厚壮汉" in duo["image_prompt"]
    # 镜像命名空间同步落盘
    import shutil as _sh

    assert (tmp_path / f"{name} E01" / "script.json").is_file()
    _sh.rmtree(tmp_path / f"{name} E01", ignore_errors=True)
    assert not (_repo_outputs() / name).exists()
    assert not (_repo_outputs() / f"{name} E01").exists()


def test_e1_breakdown_prompt_cast_rule_and_inject_unit():
    system, user = bd.build_breakdown_prompt("剧", 60, "9:16", "8", "写实", "原文",
                                             cast_plan=["阿雪", "阿牛"])
    assert "阿雪" in user and "cast" in user
    system2, user2 = bd.build_breakdown_prompt("剧", 60, "9:16", "8", "写实", "原文")
    assert "【本集出场（cast_plan" not in user2  # 无参回退：无出场名单段
    assert "【本集出场（cast_plan" in user  # 有名单则注入
    # coerce 透传 cast，非标回落 []
    raw = {"clips": [{"narration": "旁白",
                      "cast": ["阿雪", " ", 7],
                      "image_prompt": "[主体]x+[场景]x+[风格]x+[光照]x+[构图]x+[质量]1K",
                      "video_prompt": "[主体]x+[动作]x+[场景]x+[运镜]x+[光照]x+[风格]x"}]}
    sc = bd.coerce_script(raw, "剧", 8, "9:16", "8", "写实")
    assert sc["clips"][0]["cast"] == ["阿雪", "7"]
    raw2 = {"clips": [{"narration": "旁白",
                       "cast": "阿雪",
                       "image_prompt": "[主体]x+[场景]x+[风格]x+[光照]x+[构图]x+[质量]1K",
                       "video_prompt": "[主体]x+[动作]x+[场景]x+[运镜]x+[光照]x+[风格]x"}]}
    assert bd.coerce_script(raw2, "剧", 8, "9:16", "8", "写实")["clips"][0]["cast"] == []
    # 注入：单角色截 60、多角色分号连、总长 200 封顶、六段式不破
    p = bd.inject_cast_appearance("[主体]少年+[场景]雪夜+[风格]写实+[光照]月光+[构图]竖构图+[质量]1K",
                                  ["阿雪：" + "美" * 100])
    _seg = p.split("出场：", 1)[1].split("+[", 1)[0]
    assert len(_seg) <= 60 and "美" in _seg  # 单角色截 60 字
    for seg in ("[主体]", "[场景]", "[风格]", "[光照]", "[构图]", "[质量]"):
        assert seg in p
    p2 = bd.inject_cast_appearance("[主体]少年+[场景]雪夜+[风格]写实+[光照]月光+[构图]竖构图+[质量]1K",
                                   ["甲：" + "a" * 150, "乙：" + "b" * 150])
    assert "；" in p2 and len(p2.split("出场：", 1)[1].split("+[", 1)[0]) <= 200
    assert bd.inject_cast_appearance("无主体段", ["甲：短描"]) == "无主体段"
    assert not (_repo_outputs() / "E1UNIT").exists()


def test_e1_runner_reference_chain_tmp(tmp_path):
    import base64 as _b64

    import runner as rn

    drama = tmp_path / "RUNSER E01"
    sdir = tmp_path / "RUNSER"
    (drama / "characters").mkdir(parents=True, exist_ok=True)
    (sdir / "characters").mkdir(parents=True, exist_ok=True)
    (drama / "characters" / "c01.png").write_bytes(b"\x89PNG\r\n\x1a\nlocal")
    (sdir / "characters" / "g.png").write_bytes(b"\x89PNG\r\n\x1a\nglobal")
    lib = [{"id": "c01", "name": "阿雪", "aliases": ["雪姑娘"],
            "images": ["characters/c01.png"]}]
    # 回退链：clip.images 空 + cast 有名 → 用角色主图（drama 目录解析）
    clip = {"id": "s01", "images": [], "cast": ["雪姑娘"]}
    cands = rn.reference_images_for_clip(clip, {"character_refs": []},
                                         drama, drama_name="RUNSER E01",
                                         characters=lib)
    assert cands == ["characters/c01.png"]
    usable, failed = rn.resolve_reference_images(cands, drama)
    assert failed == [] and len(usable) == 1
    assert usable[0].startswith("data:image/png;base64,")
    assert _b64.b64decode(usable[0].split(",", 1)[1]) == b"\x89PNG\r\n\x1a\nlocal"
    # 显式 clip.images 优先
    clip2 = {"id": "s01", "images": ["characters/c01.png"], "cast": ["阿雪"]}
    assert rn.reference_images_for_clip(clip2, {}, drama, characters=lib) == ["characters/c01.png"]
    # cast 无命中 → script.character_refs（系列目录解析）
    clip3 = {"id": "s01", "images": [], "cast": ["路人"]}
    cands3 = rn.reference_images_for_clip(
        clip3, {"character_refs": ["characters/g.png"]}, drama,
        drama_name="RUNSER E01", characters=lib)
    assert cands3 == ["characters/g.png"]
    usable3, failed3 = rn.resolve_reference_images(
        cands3, drama, series_dir=rn._series_dir_for_drama("RUNSER E01", drama))
    assert failed3 == [] and usable3[0].startswith("data:image/png;base64,")
    assert rn._series_dir_for_drama("RUNSER E01", drama) == sdir
    # 远程原样透传；缺文件记 failed
    u4, f4 = rn.resolve_reference_images(["https://x/y.png"], drama)
    assert u4 == ["https://x/y.png"] and f4 == []
    u5, f5 = rn.resolve_reference_images(["characters/nope.png"], drama, series_dir=sdir)
    assert u5 == [] and f5 == ["characters/nope.png"]
    assert not (_repo_outputs() / "RUNSER").exists()
    assert not (_repo_outputs() / "RUNSER E01").exists()


def test_e1_locks_and_drama_zero_break_tmp(monkeypatch, tmp_path):
    from providers.agnes_image import build_image_payload
    from providers.agnes_video import build_video_payload

    # 图锁：1K + response_format 只在 extra_body + 无 tags/无顶层
    p = build_image_payload("[主体]x+[场景]x+[风格]x+[光照]x+[构图]x+[质量]1K", "1K", "9:16")
    assert p["size"] == "1K" and p["extra_body"] == {"response_format": "url"}
    assert "response_format" not in p and "tags" not in p
    # 视频锁：720P + seconds 字符串 4-12
    v = build_video_payload("prompt", "8", mode="text")
    assert v["size"] == "720P" and v["seconds"] == "8"
    with pytest.raises(ValueError):
        build_video_payload("prompt", "3", mode="text")
    with pytest.raises(ValueError):
        build_video_payload("prompt", "13", mode="text")
    with pytest.raises(ValueError):
        build_video_payload("prompt", "8", mode="text", size="1080P")
    with pytest.raises(ValueError):
        build_video_payload("prompt", "8", mode="reference",
                            images=[f"https://x/{i}.png" for i in range(6)])
    import orchestrator as _orch

    with pytest.raises(ValueError):
        _orch.validate_script({"title": "t", "total_seconds": 0,
                               "aspect": "9:16", "resolution": "720x1280",
                               "clip_seconds": "8", "character_refs": [],
                               "clips": []})
    # /api/drama/* 零破坏：new + state 在 tmp 内跑通
    c, m, pool = _b1_client(monkeypatch, tmp_path)
    script = {"title": "零破坏", "total_seconds": 60, "aspect": "9:16",
              "resolution": "720x1280", "clip_seconds": "8",
              "character_refs": [],
              "clips": _canned_script_clips()}
    r = c.post("/api/drama/new", json={"name": "E1NOBREAK", "script": script})
    assert r.status_code == 200, r.text
    r = c.get("/api/drama/E1NOBREAK/state")
    assert r.status_code == 200, r.text
    assert r.json()["script"]["total_seconds"] == pytest.approx(60.0)
    assert (tmp_path / "E1NOBREAK" / "script.json").is_file()
    assert not (_repo_outputs() / "E1NOBREAK").exists()


# ---------- E1-Fix 返工（data 白名单/5MB 上限/摘要逐行/手工合并粘性） ----------

def test_e1fix_data_url_whitelist_tmp(tmp_path):
    import runner as rn

    drama = tmp_path / "E1FIXDATA"
    drama.mkdir(parents=True, exist_ok=True)
    tiny_b64 = base64.b64encode(b"fakepng").decode()
    # 合法 data:（png/jpeg/webp）原样透传
    for mime in ("image/png", "image/jpeg", "image/webp"):
        url = f"data:{mime};base64,{tiny_b64}"
        assert rn._is_data_url(url) is True
        u, f = rn.resolve_reference_images([url], drama)
        assert u == [url] and f == []
    # 非图片 data: 拒绝 → failed（拿 Key 前失败，不烧配额）
    for bad in (f"data:text/plain;base64,{tiny_b64}",
                "data:application/json;base64,e30=",
                "data:text/html,<h1>x</h1>"):
        assert rn._is_data_url(bad) is False
        u, f = rn.resolve_reference_images([bad], drama)
        assert u == [] and f == [bad]
    assert not (_repo_outputs() / "E1FIXDATA").exists()


def test_e1fix_ref_image_size_cap_tmp(tmp_path):
    import runner as rn

    drama = tmp_path / "E1FIXCAP"
    (drama / "characters").mkdir(parents=True, exist_ok=True)
    small = drama / "characters" / "small.png"
    small.write_bytes(b"\x89PNG\r\n\x1a\ntiny")
    big = drama / "characters" / "big.png"
    big.write_bytes(b"\x00" * (rn.REF_IMAGE_MAX_BYTES + 1024))
    # 小图可用；大图记单图不可用、不抛异常、不整镜失败
    u, f = rn.resolve_reference_images(
        ["characters/small.png", "characters/big.png"], drama)
    assert len(u) == 1 and u[0].startswith("data:image/png;base64,")
    assert f == ["characters/big.png"]
    # 仅大图：usable 为空、failed 点名
    u2, f2 = rn.resolve_reference_images(["characters/big.png"], drama)
    assert u2 == [] and f2 == ["characters/big.png"]
    assert not (_repo_outputs() / "E1FIXCAP").exists()


def test_e1fix_summarize_per_role_and_footer():
    # 全量放得下：原样返回，无尾注
    chars = [_e1_char(f"角色{i:02d}", i) for i in range(3)]
    full = ch.summarize_characters(chars, limit=10000)
    for i in range(3):
        assert f"角色{i:02d}" in full
    assert "共" not in full or "已列" not in full
    # 放不下：降级每角色一行（name+首图标记+一句话），全员可列则无尾注
    long_chars = [_e1_char(f"长角{i:02d}", i, appearance="美" * 200,
                           personality="冷" * 100,
                           logline=f"小传{i:02d}" + "勤勇" * 25)
                  for i in range(5)]
    compact = ch.summarize_characters(long_chars, limit=1000)
    for i in range(5):
        assert f"长角{i:02d}" in compact
    assert len(compact) <= 1000
    # 仍超长：截断 + 尾注“共 N 个角色，已列 M 个”，已列行完整
    tiny = ch.summarize_characters(long_chars, limit=120)
    assert "共 5 个角色，已列" in tiny
    assert len(tiny) <= 120
    listed = int(tiny.split("已列")[1].split("个")[0].strip())
    assert 0 < listed < 5
    for i in range(listed):
        assert f"长角{i:02d}" in tiny
    assert not (_repo_outputs() / "E1FIXSUM").exists()


def test_e1fix_manual_merge_status_sticky_tmp(monkeypatch, tmp_path):
    # 手工 merge 与自动 merge 粘性对齐：target 已确认保持；
    # target 待确认 + source 已确认 → 升级为已确认（不回退）。
    c, m, pool = _b1_client(monkeypatch, tmp_path)
    name = "E1FIXMERGE"
    assert c.post("/api/series", json={"title": name}).status_code == 200
    r1 = c.post(f"/api/series/{name}/characters", json={"name": "甲", "desc": "甲传"})
    r2 = c.post(f"/api/series/{name}/characters", json={"name": "乙", "desc": "乙传"})
    id1, id2 = r1.json()["character"]["id"], r2.json()["character"]["id"]
    # target 待确认 + source 已确认 → 升级
    assert c.patch(f"/api/series/{name}/characters/{id1}",
                   json={"status": "已确认"}).status_code == 200
    r = c.post(f"/api/series/{name}/characters/merge",
               json={"source_ids": [id1], "target_id": id2})
    assert r.status_code == 200, r.text
    assert r.json()["character"]["status"] == "已确认"
    # target 已确认保持：再合一个待确认源不回退
    r3 = c.post(f"/api/series/{name}/characters", json={"name": "丙", "desc": "丙传"})
    id3 = r3.json()["character"]["id"]
    r = c.post(f"/api/series/{name}/characters/merge",
               json={"source_ids": [id3], "target_id": id2})
    assert r.status_code == 200, r.text
    assert r.json()["character"]["status"] == "已确认"
    assert not (_repo_outputs() / name).exists()


# ---------- FG grounding/从严合并/分镜归一（全 tmp 隔离） ----------

def test_fg_prompt_grounding_rules():
    system, user = ch.build_character_prompt("哈利出现了" * 100)
    assert "characters" in user
    assert "逐字原形" in user and "音译" in user
    assert "出现次数最多" in user
    s2, u2 = bd.build_breakdown_prompt("剧", 60, "9:16", "8", "写实", "原文",
                                       characters_summary="- 哈利：短发",
                                       cast_plan=["哈利"])
    assert "旁白" in u2 and "可用名清单" in u2
    assert "原样照抄" in u2
    assert not (_repo_outputs() / "FGUNIT").exists()


def test_fg_canonicalize_transliteration_unit():
    items = [{"id": "c01", "name": "hally", "aliases": ["哈利"],
              "logline": "", "appearance": "短发", "personality": "",
              "relation": "", "outfit": "", "status": "待确认",
              "images": [], "voice": {}, "first_seen": "E01",
              "notes": "", "locked": False}]
    src = "哈利走进教室。哈利笑了。哈利又来了。"
    kept, skipped = ch.canonicalize_names(items, src)
    assert skipped == 0 and len(kept) == 1
    assert kept[0]["name"] == "哈利"
    # 并列取原 name：甲/乙各 1 次 → 保原名
    tie = [{"name": "甲", "aliases": ["乙"]}]
    kept2, skipped2 = ch.canonicalize_names(tie, "甲乙")
    assert skipped2 == 0 and kept2[0]["name"] == "甲"
    assert set(kept2[0]["aliases"]) == {"乙"}
    assert not (_repo_outputs() / "FGUNIT").exists()


def test_fg_canonicalize_all_skipped_unit():
    items = [{"name": "hally", "aliases": ["donk"]},
             {"name": "外星人XYZ", "aliases": ["不存在ABC"]}]
    kept, skipped = ch.canonicalize_names(items, "哈利和护士在医院。")
    assert kept == [] and skipped == 2
    assert not (_repo_outputs() / "FGUNIT").exists()


def test_fg_merge_strict_no_fuzzy():
    # 近似名不并（一字之差）
    assert len(ch.merge_characters([_char("哈利", id="c01")],
                                   [_char("哈里")])) == 2
    # 护士三写法无别名交集不自动并
    assert len(ch.merge_characters([_char("护士", id="c01")],
                                   [_char("女护士")])) == 2
    assert len(ch.merge_characters([_char("女护士", id="c01")],
                                   [_char("医护人员")])) == 2
    # 大小写敏感
    assert len(ch.merge_characters([_char("Alice", id="c01")],
                                   [_char("alice")])) == 2
    # 内空白不归一（strip 后比对，不去全空白）
    assert len(ch.merge_characters([_char("阿雪", id="c01")],
                                   [_char("阿 雪")])) == 2
    # 包含不合并（“雪”≠“阿雪”）
    assert len(ch.merge_characters([_char("阿雪", id="c01")],
                                   [_char("雪")])) == 2
    # 真命中仍合并三分支
    assert len(ch.merge_characters([_char("阿雪", id="c01")],
                                   [_char("阿雪")])) == 1
    assert len(ch.merge_characters([_char("甲", id="c01", aliases=["X"])],
                                   [_char("乙", aliases=["X"])])) == 1
    assert len(ch.merge_characters([_char("甲", id="c01", aliases=["乙"])],
                                   [_char("乙")])) == 1
    # strip 后相等仍合并；locked 粘性保持
    assert len(ch.merge_characters([_char("阿雪", id="c01", locked=True)],
                                   [_char(" 阿雪 ", appearance="新")])) == 1
    assert not (_repo_outputs() / "FGUNIT").exists()


def test_fg_extract_grounding_route_tmp(monkeypatch, tmp_path):
    import json as _json

    c, m, pool = _b1_client(monkeypatch, tmp_path)
    name = "FGEXTRACT"
    assert c.post("/api/series", json={"title": name}).status_code == 200
    src = "哈利走进教室。哈利笑了。护士查房。" * 60
    assert c.post(f"/api/series/{name}/episodes", json={
        "title": "首集", "total_seconds": 60, "source_text": src}).status_code == 200

    def _fake(prompt, **kw):
        return {"text": _json.dumps({"characters": [
            {"name": "hally", "aliases": ["哈利"],
             "appearance": "短发少年", "personality": "勇敢"},
            {"name": "外星人XYZ", "aliases": ["不存在ABC"],
             "appearance": "绿皮肤"},
        ]}, ensure_ascii=False), "generated_by": "fake/fg@now"}

    monkeypatch.setattr(m, "_generate_text", _fake)
    r = c.post(f"/api/series/{name}/characters/extract", json={"episode_id": "E01"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skipped"] == 1
    assert body["count"] == 1
    assert "grounded" in body.get("note", "") and "skipped" in body.get("note", "")
    shown = {x["name"]: x for x in body["characters"]}
    assert "哈利" in shown and "hally" not in shown
    assert shown["哈利"]["first_seen"] == "E01"
    assert not (_repo_outputs() / name).exists()


def test_fg_extract_all_skipped_route_tmp(monkeypatch, tmp_path):
    import json as _json

    c, m, pool = _b1_client(monkeypatch, tmp_path)
    name = "FGSKIPALL"
    assert c.post("/api/series", json={"title": name}).status_code == 200
    assert c.post(f"/api/series/{name}/episodes", json={
        "title": "首集", "total_seconds": 60,
        "source_text": "哈利和护士在医院。" * 60}).status_code == 200

    def _fake(prompt, **kw):
        return {"text": _json.dumps({"characters": [
            {"name": "hally", "aliases": ["donk"], "appearance": "短发"},
            {"name": "外星人XYZ", "aliases": [], "appearance": "绿皮肤"},
        ]}, ensure_ascii=False), "generated_by": "fake/fg@now"}

    monkeypatch.setattr(m, "_generate_text", _fake)
    r = c.post(f"/api/series/{name}/characters/extract", json={"episode_id": "E01"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 0 and body["characters"] == []
    assert body["skipped"] == 2
    assert "grounded" in body.get("note", "")
    assert not (_repo_outputs() / name).exists()


def _fg_canned_bd_clips_three_branches():
    durs = [8] * 7 + [4]
    # s01 别名命中→映射库名；s02 原文有但不在库→保留+进cast；s03 两无→旁白
    speakers = ["哈利·波特", "女护士", "外星人",
                "旁白", "旁白", "旁白", "旁白", "旁白"]
    clips = []
    cursor = 0.0
    for i, d in enumerate(durs):
        cast = [] if i != 0 else ["哈利"]
        clips.append({"id": f"s{i + 1:02d}", "start": cursor, "duration": d,
                      "narration": f"第{i + 1}镜旁白",
                      "speaker": speakers[i], "cast": cast,
                      "image_prompt": "[主体]少年+[场景]雪夜+[风格]写实+[光照]月光+[构图]竖构图+[质量]1K,高细节",
                      "video_prompt": "[主体]少年+[动作]抬头+[场景]雪夜+[运镜]缓慢推镜+[光照]月光+[风格]写实",
                      "video_mode": "text"})
        cursor += d
    return clips


def test_fg_breakdown_speaker_normalize_tmp(monkeypatch, tmp_path):
    import json as _json

    c, m, pool = _b1_client(monkeypatch, tmp_path)
    name = "FGBD"
    assert c.post("/api/series", json={"title": name}).status_code == 200
    src = "哈利走进病房。女护士查房。哈利向女护士道谢。" * 60
    assert c.post(f"/api/series/{name}/episodes", json={
        "title": "首集", "total_seconds": 60, "source_text": src}).status_code == 200
    lib = [_e1_char("哈利", 0), _e1_char("护士长", 1)]
    lib[0].update({"id": "c01", "aliases": ["哈利·波特"],
                   "appearance": "黑发少年", "images": []})
    lib[1].update({"id": "c02", "aliases": [],
                   "appearance": "白衣护士长", "images": []})
    ch.save_characters(name, [ch.normalize_character(x) for x in lib],
                       base_dir=tmp_path)

    def _fake(prompt, **kw):
        assert "旁白" in prompt and "原样照抄" in prompt
        return {"text": _json.dumps(
            {"title": "首集", "total_seconds": 60, "aspect": "9:16",
             "resolution": "720x1280", "clip_seconds": "8",
             "character_refs": [], "clips": _fg_canned_bd_clips_three_branches()},
            ensure_ascii=False), "generated_by": "fake/fgbd@now"}

    monkeypatch.setattr(m, "_generate_text", _fake)
    r = c.post(f"/api/series/{name}/episodes/E01/breakdown",
               json={"clip_seconds": "8"})
    assert r.status_code == 200, r.text
    body = r.json()
    clips = body["script"]["clips"]
    assert len(clips) == 8
    # 分支1：别名→库名
    assert clips[0]["speaker"] == "哈利"
    assert clips[0]["cast"] == ["哈利"]
    # 分支2：原文有但不在库→保留原字并进 cast
    assert clips[1]["speaker"] == "女护士"
    assert "女护士" in clips[1]["cast"]
    # 分支3：两无→旁白
    assert clips[2]["speaker"] == "旁白"
    assert "旁白" in body.get("note", "")
    # 单镜时长仍 4-12 且求和 60
    assert all(4 <= float(x["duration"]) <= 12 for x in clips)
    assert abs(sum(float(x["duration"]) for x in clips) - 60) < 1e-6
    import shutil as _sh

    _sh.rmtree(tmp_path / f"{name} E01", ignore_errors=True)
    assert not (_repo_outputs() / name).exists()
    assert not (_repo_outputs() / f"{name} E01").exists()


def test_fg_locks_and_drama_zero_break_tmp(monkeypatch, tmp_path):
    from providers.agnes_image import build_image_payload
    from providers.agnes_video import build_video_payload

    p = build_image_payload("[主体]x+[场景]x+[风格]x+[光照]x+[构图]x+[质量]1K", "1K", "9:16")
    assert p["size"] == "1K" and p["extra_body"] == {"response_format": "url"}
    assert "response_format" not in p and "tags" not in p
    v = build_video_payload("prompt", "8", mode="text")
    assert v["size"] == "720P" and v["seconds"] == "8"
    with pytest.raises(ValueError):
        build_video_payload("prompt", "3", mode="text")
    c, m, pool = _b1_client(monkeypatch, tmp_path)
    script = {"title": "零破坏", "total_seconds": 60, "aspect": "9:16",
              "resolution": "720x1280", "clip_seconds": "8",
              "character_refs": [],
              "clips": _canned_script_clips()}
    r = c.post("/api/drama/new", json={"name": "FGNOBREAK", "script": script})
    assert r.status_code == 200, r.text
    r = c.get("/api/drama/FGNOBREAK/state")
    assert r.status_code == 200, r.text
    assert r.json()["script"]["total_seconds"] == pytest.approx(60.0)
    assert (tmp_path / "FGNOBREAK" / "script.json").is_file()
    assert not (_repo_outputs() / "FGNOBREAK").exists()


def test_merge_additive_rerun_idempotent():
    """同集重跑：条目逐一等价（没有就不变），只 Sat 别名/立绘并入。"""
    existing = [_char("阿雪", id="c01", appearance="短发少女",
                      aliases=["雪姑娘"], relation="师门", outfit="白衣",
                      personality="", first_seen="E01", status="已确认")]
    incoming = [_char("阿雪", appearance="长发女王", personality="活泼",
                      aliases=["雪姑娘", "小雪"], images=["characters/c01_x.png"],
                      first_seen="E02")]
    once = ch.merge_characters(existing, incoming)
    assert len(once) == 1
    c = once[0]
    assert c["appearance"] == "短发少女"  # 不覆盖
    assert c["relation"] == "师门"  # 不覆盖
    assert c["outfit"] == "白衣"  # 不覆盖
    assert c["personality"] == "活泼"  # 空字段补齐
    assert c["first_seen"] == "E01"  # 不后移
    assert c["status"] == "已确认"  # 粘性
    assert "小雪" in c["aliases"] and "characters/c01_x.png" in c["images"]
    twice = ch.merge_characters(once, incoming)
    assert twice == once  # 再跑一次完全等价


def test_merge_additive_api_note_tmp(monkeypatch, tmp_path):
    """API 级：第二集无新角色 → 旧字段不动，note 报新增0/沿用N。"""
    import json as _json

    c, m, pool = _b1_client(monkeypatch, tmp_path)
    name = "ADDITIVE"
    assert c.post("/api/series", json={"title": name}).status_code == 200
    src = "阿雪仗剑下山东克守夜" * 100
    assert c.post(f"/api/series/{name}/episodes", json={
        "title": "首集", "total_seconds": 60, "source_text": src}).status_code == 200

    def _fake_v1(prompt, **kw):
        return {"text": _json.dumps({"characters": [
            {"name": "阿雪", "aliases": ["雪姑娘"], "appearance": "短发少女"},
        ]}, ensure_ascii=False), "generated_by": "fake/ch@now"}

    monkeypatch.setattr(m, "_generate_text", _fake_v1)
    r = c.post(f"/api/series/{name}/characters/extract", json={"episode_id": "E01"})
    assert r.status_code == 200, r.text
    assert "新增1" in r.json().get("note", "") and "沿用0" in r.json().get("note", "")

    def _fake_v2(prompt, **kw):
        return {"text": _json.dumps({"characters": [
            {"name": "阿雪", "aliases": ["雪姑娘", "小雪"],
             "appearance": "长发女王", "personality": "活泼"},
        ]}, ensure_ascii=False), "generated_by": "fake/ch@now"}

    monkeypatch.setattr(m, "_generate_text", _fake_v2)
    r = c.post(f"/api/series/{name}/characters/extract", json={"episode_id": "E01"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "新增0" in body.get("note", "") and "沿用1" in body.get("note", "")
    shown = {x["name"]: x for x in body["characters"]}
    assert shown["阿雪"]["appearance"] == "短发少女"  # 旧的不变
    assert shown["阿雪"]["personality"] == "活泼"  # 空的补齐
    assert "小雪" in shown["阿雪"]["aliases"]  # 新别名并入
    assert not (_repo_outputs() / name).exists()


# ---------- U1 立绘上传（multipart 单文件 file + make_main；全 tmp 隔离） ----------

def _u1_has_multipart() -> bool:
    try:
        import multipart  # noqa: F401
        return True
    except ImportError:
        pass
    try:
        import python_multipart  # noqa: F401
        return True
    except ImportError:
        return False


def _u1_new_character(c, name, cname="阿雪"):
    r = c.post(f"/api/series/{name}/characters",
               json={"name": cname, "desc": "立绘模特"})
    assert r.status_code == 200, r.text
    return r.json()["character"]["id"]


def _u1_files(c, name, cid, filename, data, extra=None):
    kw: dict = {"files": {"file": (filename, data, "image/png")}}
    if extra is not None:
        kw["data"] = extra
    return c.post(f"/api/series/{name}/characters/{cid}/images", **kw)


def test_u1_upload_make_main_first_tmp(monkeypatch, tmp_path):
    c, m, pool = _b1_client(monkeypatch, tmp_path)
    if not _u1_has_multipart():
        pytest.skip("python-multipart 缺失，跳过 multipart 上传断言")
    name = "U1UPMAIN"
    assert c.post("/api/series", json={"title": name}).status_code == 200
    cid = _u1_new_character(c, name)
    # 先放一张旧图，再默认 make_main（不传字段）→ 新图插 images[0]
    assert c.patch(f"/api/series/{name}/characters/{cid}",
                   json={"images": ["characters/old.png"]}).status_code == 200
    r = _u1_files(c, name, cid, "avatar.PNG", b"\x89PNG\r\n\x1a\nupload1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["path"].startswith("characters/")
    assert body["path"].lower().endswith(".png")  # 后缀大小写不敏感
    assert ".." not in body["path"] and "/" not in body["path"][len("characters/"):]
    assert body["character"]["images"][0] == body["path"]
    assert body["character"]["images"][1] == "characters/old.png"
    assert body["character"]["portrait_url"].startswith("/api/")
    assert "files?path=" in body["character"]["portrait_url"]
    assert (tmp_path / name / body["path"]).is_file()
    assert not (_repo_outputs() / name).exists()


def test_u1_upload_append_and_rejects_tmp(monkeypatch, tmp_path):
    c, m, pool = _b1_client(monkeypatch, tmp_path)
    if not _u1_has_multipart():
        pytest.skip("python-multipart 缺失，跳过 multipart 上传断言")
    import runner as rn

    name = "U1UPAPPEND"
    assert c.post("/api/series", json={"title": name}).status_code == 200
    cid = _u1_new_character(c, name)
    r = _u1_files(c, name, cid, "first.png", b"\x89PNG\r\n\x1a\nfirst",
                  {"make_main": "true"})
    assert r.status_code == 200, r.text
    first = r.json()["path"]
    # make_main=false → append 到末尾
    r = _u1_files(c, name, cid, "second.jpg", b"\xff\xd8\xffsecond",
                  {"make_main": "false"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["character"]["images"][0] == first
    assert body["character"]["images"][-1] == body["path"]
    assert body["path"].lower().endswith(".jpg")
    # 非法后缀 → 400
    assert _u1_files(c, name, cid, "evil.exe", b"x").status_code == 400
    assert _u1_files(c, name, cid, "noext", b"x").status_code == 400
    # 缺 file 字段 → 400
    r = c.post(f"/api/series/{name}/characters/{cid}/images",
               files={"other": ("a.png", b"\x89PNG", "image/png")})
    assert r.status_code == 400
    # 非 multipart（JSON）→ 400
    assert c.post(f"/api/series/{name}/characters/{cid}/images",
                  json={"file": "x"}).status_code == 400
    # 超大（读 runner 常量）→ 400
    big = b"\x00" * (rn.REF_IMAGE_MAX_BYTES + 1)
    assert _u1_files(c, name, cid, "big.png", big).status_code == 400
    # 穿越文件名：仍 200，落盘固定名不出 characters/
    r = _u1_files(c, name, cid, "../../evil.png", b"\x89PNG\r\n\x1a\ntrav",
                  {"make_main": "false"})
    assert r.status_code == 200, r.text
    p = r.json()["path"]
    assert ".." not in p and p.startswith("characters/")
    assert (tmp_path / name / p).is_file()
    # 系列/角色不存在 → 404
    assert _u1_files(c, "NOSUCHSERIES", cid, "a.png",
                     b"\x89PNG").status_code == 404
    assert _u1_files(c, name, "c99", "a.png",
                     b"\x89PNG").status_code == 404
    assert not (_repo_outputs() / name).exists()
    assert not (_repo_outputs() / "NOSUCHSERIES").exists()


def test_u1_upload_missing_lib_400_tmp(monkeypatch, tmp_path):
    # 缺 python-multipart 时 multipart 直接 400 指引安装（与 episode 上传惯例一致）
    import asyncio

    import main as _m

    _patch_outputs_to_tmp(monkeypatch, tmp_path)
    name = "U1FIXMP"
    se.create_series(name, base_dir=tmp_path)
    ch.save_characters(name, [ch.normalize_character(
        {"id": "c01", "name": "阿雪"})], base_dir=tmp_path)

    class _FakeReq:
        headers = {"content-type": "multipart/form-data; boundary=----x"}
        query_params: dict = {}

        async def form(self):
            raise AssertionError(
                "Form data requires 'python-multipart' to be installed.")

        async def body(self):
            return b"------x\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.png\"\r\n\r\nxx\r\n------x--\r\n"

        async def json(self):
            return {}

    with pytest.raises(Exception) as exc:
        asyncio.run(_m.series_character_image_upload(name, "c01", _FakeReq()))  # type: ignore[arg-type]
    from fastapi import HTTPException as _HTTP

    assert isinstance(exc.value, _HTTP)
    assert exc.value.status_code == 400
    assert "python-multipart" in exc.value.detail
    assert not (_repo_outputs() / name).exists()


# ---------- Novel 全文直调 extract 加法合并（Builder B；全 tmp 隔离） ----------

def test_novel_extract_source_text_merge_tmp(monkeypatch, tmp_path):
    """整本小说 source_text 直调 extract 两次：加法合并、只填空不覆盖、first_seen 全文。"""
    import json as _json

    # tmp 隔离经 _b1_client -> _patch_all_roots_to_tmp
    # （含 _patch_outputs_to_tmp 的 series/main 双 root 重定向到 tmp_path）
    c, m, pool = _b1_client(monkeypatch, tmp_path)
    name = "NOVELMERGE"
    assert c.post("/api/series", json={"title": name}).status_code == 200

    def _fake_v1(prompt, **kw):
        return {"text": _json.dumps({"characters": [{
            "name": "阿雪", "aliases": [], "logline": "白衣少女",
            "appearance": "白衣长发", "personality": "冷静",
            "relation": "", "outfit": "", "status": "待确认",
            "voice": {"provider": "", "voice_id": ""},
            "first_seen": "", "notes": ""}]}, ensure_ascii=False),
            "generated_by": "fake/novel@now"}

    monkeypatch.setattr(m, "_generate_text", _fake_v1)
    r1 = c.post(f"/api/series/{name}/characters/extract", json={
        "source_text": "阿雪在雪夜下山……（含名长文本）",
        "first_seen": "全文"})
    assert r1.status_code == 200, r1.text

    def _fake_v2(prompt, **kw):
        return {"text": _json.dumps({"characters": [
            {"name": "阿雪", "aliases": [], "logline": "",
             "appearance": "", "personality": "",
             "relation": "", "outfit": "", "status": "待确认",
             "voice": {"provider": "", "voice_id": ""},
             "first_seen": "", "notes": ""},
            {"name": "老周", "aliases": [], "logline": "山门守夜人",
             "appearance": "灰袍长须", "personality": "沉稳",
             "relation": "", "outfit": "", "status": "待确认",
             "voice": {"provider": "", "voice_id": ""},
             "first_seen": "", "notes": ""},
        ]}, ensure_ascii=False), "generated_by": "fake/novel@now"}

    monkeypatch.setattr(m, "_generate_text", _fake_v2)
    r2 = c.post(f"/api/series/{name}/characters/extract", json={
        "source_text": "老周与阿雪在山门相遇……",
        "first_seen": "全文"})
    assert r2.status_code == 200, r2.text

    r = c.get(f"/api/series/{name}/characters")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    shown = {x["name"]: x for x in body["characters"]}
    assert set(shown) == {"阿雪", "老周"}
    assert shown["阿雪"]["appearance"] == "白衣长发"  # 只填空不覆盖
    assert shown["阿雪"]["first_seen"] == "全文"
    assert shown["老周"]["first_seen"] == "全文"
    assert not (_repo_outputs() / name).exists()


# ---------- 场景库：CRUD + 上传 + AI 生成 + 分镜资产注入（全 tmp 隔离） ----------

def test_scenes_crud_tmp(monkeypatch, tmp_path):
    c, m, pool = _b1_client(monkeypatch, tmp_path)
    name = "SCENECRUD"
    assert c.post("/api/series", json={"title": name}).status_code == 200
    r = c.get(f"/api/series/{name}/scenes")
    assert r.status_code == 200 and r.json()["scenes"] == []
    r = c.post(f"/api/series/{name}/scenes",
               json={"name": "雪夜山门", "description": "青石台阶"})
    assert r.status_code == 200, r.text
    sid = r.json()["scene"]["id"]
    assert r.json()["scene"]["image_url"] == ""
    # 同名拒绝、空名拒绝、超长描述拒绝
    assert c.post(f"/api/series/{name}/scenes",
                 json={"name": "雪夜山门"}).status_code == 400
    assert c.post(f"/api/series/{name}/scenes",
                 json={"name": "   "}).status_code == 400
    assert c.post(f"/api/series/{name}/scenes",
                 json={"name": "x", "description": "y" * 201}).status_code == 400
    r = c.patch(f"/api/series/{name}/scenes/{sid}",
                json={"description": "青石台阶，冷月"})
    assert r.status_code == 200, r.text
    assert r.json()["scene"]["description"] == "青石台阶，冷月"
    assert c.patch(f"/api/series/{name}/scenes/none",
                   json={"description": "x"}).status_code == 404
    assert c.delete(f"/api/series/{name}/scenes/{sid}").status_code == 200
    assert c.get(f"/api/series/{name}/scenes").json()["scenes"] == []
    assert c.delete(f"/api/series/{name}/scenes/{sid}").status_code == 404
    assert c.get("/api/series/NOSUCH/scenes").status_code == 404
    assert not (_repo_outputs() / name).exists()


def test_scenes_upload_and_generate_tmp(monkeypatch, tmp_path):
    c, m, pool = _b1_client(monkeypatch, tmp_path)
    if not _u1_has_multipart():
        pytest.skip("python-multipart 缺失，跳过 multipart 上传断言")
    name = "SCENEUP"
    assert c.post("/api/series", json={"title": name}).status_code == 200
    r = c.post(f"/api/series/{name}/scenes",
               json={"name": "雪夜山门", "description": "青石台阶"})
    assert r.status_code == 200, r.text
    sid = r.json()["scene"]["id"]
    # 上传：默认 make_main 插首位
    r = c.post(f"/api/series/{name}/scenes/{sid}/images",
               files={"file": ("bg.PNG", b"\x89PNG\r\n\x1a\nbg1", "image/png")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"].startswith("scenes/")
    assert body["path"].lower().endswith(".png")
    assert ".." not in body["path"]
    assert body["scene"]["images"][0] == body["path"]
    assert body["scene"]["image_url"].startswith("/api/")
    assert (tmp_path / name / body["path"]).is_file()
    # 非法后缀/缺 file/非 multipart/超大 → 400；未知场景 → 404
    assert c.post(f"/api/series/{name}/scenes/{sid}/images",
                 files={"file": ("e.exe", b"x", "image/png")}).status_code == 400
    assert c.post(f"/api/series/{name}/scenes/{sid}/images",
                 files={"other": ("a.png", b"\x89PNG", "image/png")}).status_code == 400
    assert c.post(f"/api/series/{name}/scenes/{sid}/images",
                 json={"file": "x"}).status_code == 400
    import runner as rn
    big = b"\x00" * (rn.REF_IMAGE_MAX_BYTES + 1)
    assert c.post(f"/api/series/{name}/scenes/{sid}/images",
                 files={"file": ("big.png", big, "image/png")}).status_code == 400
    assert c.post(f"/api/series/{name}/scenes/none/images",
                 files={"file": ("a.png", b"\x89PNG", "image/png")}).status_code == 404
    # AI 生成：fake b64，size 固定 1K，不断言网络
    tiny_png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfakepng").decode()
    seen: dict = {}

    def _fake_gen(prompt, size="1K", ratio="9:16", api_key="", **kw):
        seen["size"] = size
        assert "[场景]" in prompt
        return {"b64": tiny_png_b64, "generated_by": "agnes/agnes-image-2.5-flash@now"}

    monkeypatch.setattr(m, "_generate_image", _fake_gen)
    monkeypatch.setattr(m, "_M1_generate_image", _fake_gen)
    r = c.post(f"/api/series/{name}/scenes/{sid}/image", json={"style": "写实"})
    assert r.status_code == 200, r.text
    assert seen.get("size") == "1K"
    assert r.json()["path"].startswith("scenes/")
    assert len(r.json()["scene"]["images"]) == 2  # 上传 1 + 生成 1
    assert not (_repo_outputs() / name).exists()


def test_breakdown_keeps_reference_and_reports_missing_tmp(monkeypatch, tmp_path):
    """AI 判要图即保留要图：合法引用保留，幻造路径进 missing_assets（不降级 text）。"""
    import json as _json

    c, m, pool = _b1_client(monkeypatch, tmp_path)
    name = "SHOTMODEBD"
    assert c.post("/api/series", json={"title": name}).status_code == 200
    r = c.post(f"/api/series/{name}/episodes", json={
        "title": "E1", "total_seconds": 16, "source_text": "少年雪夜下山阿雪" * 100})
    assert r.status_code == 200, r.text
    # 角色立绘 + 场景图：字符串路径即可（存在性=库内登记，不验文件）
    cid = c.post(f"/api/series/{name}/characters",
                 json={"name": "阿雪"}).json()["character"]["id"]
    assert c.patch(f"/api/series/{name}/characters/{cid}",
                   json={"images": ["characters/c01_main.png"]}).status_code == 200
    sid = c.post(f"/api/series/{name}/scenes",
                 json={"name": "雪夜山门",
                       "description": "青石台阶"}).json()["scene"]["id"]
    assert c.patch(f"/api/series/{name}/scenes/{sid}",
                   json={"images": ["scenes/s01_main.png"]}).status_code == 200

    def _fake_text(prompt, **kw):
        clips = [
            {"id": "s01", "start": 0, "duration": 8, "narration": "旁白一",
             "cast": ["阿雪"], "scene": [],
             "image_prompt": "[主体]阿雪+[场景]山门+[风格]写实+[光照]月光+[构图]竖构图+[质量]1K,高细节",
             "video_prompt": "[主体]阿雪+[动作]抬头+[场景]山门+[运镜]缓慢推镜+[光照]月光+[风格]写实",
             "video_mode": "reference", "images": ["characters/c01_main.png"]},
            {"id": "s02", "start": 8, "duration": 8, "narration": "旁白二",
             "cast": [], "scene": ["雪夜山门"],
             "image_prompt": "[主体]山门+[场景]雪夜+[风格]写实+[光照]月光+[构图]竖构图+[质量]1K,高细节",
             "video_prompt": "[主体]山门+[动作]雪落+[场景]雪夜+[运镜]缓慢推镜+[光照]月光+[风格]写实",
             "video_mode": "reference", "images": ["scenes/s99_ghost.png"]},
        ]
        return {"text": _json.dumps(
            {"title": "E1", "total_seconds": 16, "aspect": "9:16",
             "resolution": "720x1280", "clip_seconds": "8",
             "character_refs": [], "scene_refs": [], "clips": clips},
            ensure_ascii=False), "generated_by": "fake/bd@now"}

    monkeypatch.setattr(m, "_generate_text", _fake_text)
    r = c.post(f"/api/series/{name}/episodes/E01/breakdown", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    clips = body["script"]["clips"]
    assert clips[0]["video_mode"] == "reference"
    assert clips[0]["images"] == ["characters/c01_main.png"]
    # 幻造路径保留原判不降级，进 missing_assets
    assert clips[1]["video_mode"] == "reference"
    assert clips[1]["images"] == ["scenes/s99_ghost.png"]
    missing = body.get("missing_assets", [])
    assert [(x["clip_id"], x["planned_path"]) for x in missing] == \
        [("s02", "scenes/s99_ghost.png")]
    # 服务端回填双锚点 + note 点名待补图
    assert body["script"]["character_refs"] == ["characters/c01_main.png"]
    assert body["script"]["scene_refs"] == ["scenes/s01_main.png"]
    assert "待补图" in (body.get("note", "") or "")
    assert not (_repo_outputs() / name).exists()


# ---------- 分镜模式规划：runner 场景回退合并 + 首帧归一 ----------

def test_shotmode_reference_merges_scene_and_global_tmp(tmp_path):
    import runner as rn

    drama = tmp_path / "SHOTMODE E01"
    drama.mkdir(parents=True, exist_ok=True)
    lib = [{"id": "c01", "name": "阿雪", "aliases": [],
            "images": ["characters/c01_main.png"]}]
    slab = [{"id": "s01", "name": "雪夜山门",
             "images": ["scenes/s01_main.png"]}]
    script = {"character_refs": ["characters/c01_main.png"],
              "scene_refs": ["scenes/s01_main.png"]}
    # 显式 + cast + scene 合并去重
    clip = {"id": "s01", "images": ["characters/c09_extra.png"],
            "cast": ["阿雪"], "scene": ["雪夜山门"]}
    out = rn.reference_images_for_clip(clip, script, drama,
                                       drama_name="SHOTMODE E01",
                                       characters=lib, scenes=slab)
    assert out == ["characters/c09_extra.png", "characters/c01_main.png",
                   "scenes/s01_main.png"]
    # 无显式：cast 主图 + scene 主图（全局引用不再覆盖库推断）
    clip2 = {"id": "s02", "cast": ["阿雪"], "scene": ["雪夜山门"]}
    assert rn.reference_images_for_clip(
        clip2, script, drama, drama_name="SHOTMODE E01",
        characters=lib, scenes=slab) == [
        "characters/c01_main.png", "scenes/s01_main.png"]
    # scene 字符串单值兼容
    clip3 = {"id": "s03", "scene": "雪夜山门"}
    assert rn.reference_images_for_clip(
        clip3, {"character_refs": [], "scene_refs": []}, drama,
        drama_name="SHOTMODE E01", characters=[], scenes=slab) == [
        "scenes/s01_main.png"]
    # 截断 5
    big = {"id": "s04",
           "images": [f"https://x/{i}.png" for i in range(7)]}
    assert len(rn.reference_images_for_clip(big, {}, drama)) == 5
    assert not (_repo_outputs() / "SHOTMODE").exists()
    assert not (_repo_outputs() / "SHOTMODE E01").exists()


def test_shotmode_canonical_frame_tmp(tmp_path):
    import runner as rn

    drama = tmp_path / "SHOTFRAME"
    (drama / "images").mkdir(parents=True, exist_ok=True)
    (drama / "images" / "s01.png").write_bytes(b"frame")
    assert rn._canonical_frame_ref("shots/s01_last.png", drama) == "images/s01.png"
    assert rn._canonical_frame_ref("shots/s09_last.png", drama) == "shots/s09_last.png"
    assert rn._canonical_frame_ref("images/s02.png", drama) == "images/s02.png"
    assert not (_repo_outputs() / "SHOTFRAME").exists()


def test_scenes_module_validate_merge_select_tmp(tmp_path):
    import scenes as sc

    base = {"id": "s01", "name": "雪夜山门", "description": "青石台阶",
            "images": ["scenes/s01_a.png"], "first_seen": "E01",
            "notes": "", "locked": False}
    norm = sc.normalize_scene(dict(base))
    assert sc.validate_scene(norm) is True
    with pytest.raises(ValueError):
        sc.normalize_scene({"id": "s02", "name": "   "})
    with pytest.raises(ValueError):
        sc.validate_scene({**norm, "description": "y" * 201})
    # 增量合并：只并 images、描述只填空不覆盖
    merged = sc.merge_scenes(
        [norm],
        [{"id": "s09", "name": "雪夜山门", "description": "新描述",
          "images": ["scenes/s01_b.png"], "first_seen": "",
          "notes": "", "locked": False},
         {"id": "s02", "name": "竹林", "description": "",
          "images": [], "first_seen": "", "notes": "", "locked": False}])
    assert len(merged) == 2
    assert merged[0]["description"] == "青石台阶"
    assert merged[0]["images"] == ["scenes/s01_a.png", "scenes/s01_b.png"]
    # 出场优先 + 主图选取 + 落盘往返（tmp 隔离）
    refs = sc.select_scene_refs(merged, ["竹林", "雪夜山门"], 5)
    assert refs == ["scenes/s01_a.png"]  # 竹林无图跳过
    assert "雪夜山门" in sc.summarize_scenes(merged)
    assert "scenes/s01_a.png" in sc.scene_asset_lines(merged)
    saved = sc.save_scenes("SCENELIB", merged, base_dir=tmp_path)
    assert sc.load_scenes("SCENELIB", base_dir=tmp_path) == saved
    prompt = sc.build_scene_prompt(merged[0], style="写实", ratio="9:16")
    assert "[场景]" in prompt and "1K" in prompt
    assert not (_repo_outputs() / "SCENELIB").exists()
