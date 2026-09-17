"""AI 分镜契约测试（docs/04）：计划数学 / JSON 提取 / 归一化 / 接口（fake 文本）。"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import breakdown as bd  # noqa: E402


def test_clip_plan_exact():
    assert bd.clip_plan(64, "8") == [8.0] * 8


def test_clip_plan_60s_is_7x8_plus_4():
    plan = bd.clip_plan(60, "8")
    assert plan == [8.0] * 7 + [4.0]


def test_clip_plan_tail_converges():
    plan = bd.clip_plan(65, "8")
    assert len(plan) == 9
    assert plan[-1] == pytest.approx(1.0)
    assert abs(sum(plan) - 65) < 1e-6


def test_extract_json_plain_and_fenced():
    assert bd.extract_json('{"a": 1}') == {"a": 1}
    assert bd.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert bd.extract_json('好的：{"a": 1}，请查收') == {"a": 1}


def test_extract_json_rejects():
    with pytest.raises(ValueError):
        bd.extract_json("")
    with pytest.raises(ValueError):
        bd.extract_json("今天天气不错")
    with pytest.raises(ValueError):
        bd.extract_json("[1, 2]")


def _raw_clip(i, **kw):
    base = {"id": f"s{i:02d}", "narration": f"旁白{i}",
            "image_prompt": "[主体]x+[场景]x+[风格]x+[光照]x+[构图]x+[质量]1K",
            "video_prompt": "[主体]x+[动作]x+[场景]x+[运镜]x+[光照]x+[风格]x",
            "video_mode": "text"}
    base.update(kw)
    return base


def test_coerce_converges_sum_and_starts():
    raw = {"clips": [_raw_clip(1), _raw_clip(2)]}
    script = bd.coerce_script(raw, "剧", 65, "9:16", "8", "写实")
    assert len(script["clips"]) == 9  # 缺镜按计划补齐
    assert abs(sum(c["duration"] for c in script["clips"]) - 65) < 1e-6
    cursor = 0.0
    for c in script["clips"]:
        assert c["start"] == pytest.approx(cursor)
        assert c["video_mode"] == "text"
        cursor += c["duration"]
    assert script["clip_seconds"] == "8"
    assert script["resolution"] == "720x1280"


def test_coerce_drops_media_and_defaults_prompts():
    raw = {"clips": [_raw_clip(1, first_frame="a.png", images=["x"],
                               image_prompt="", video_prompt="")]}
    script = bd.coerce_script(raw, "剧", 60, "16:9", "8", "风")
    c = script["clips"][0]
    assert "first_frame" not in c and "images" not in c
    assert "[主体]" in c["image_prompt"] and "[动作]" in c["video_prompt"]
    assert script["resolution"] == "1280x720"


def test_coerce_requires_clips():
    with pytest.raises(ValueError):
        bd.coerce_script({}, "剧", 60, "9:16", "8", "")


# ---- 接口（fake 文本 Provider + stub 密钥池） ----

class _FakePool:
    def __init__(self):
        self.released = []

    def acquire(self, kind, **kw):
        assert kind == "text"
        return types.SimpleNamespace(id="k1", raw_key="sk-fake")

    def release(self, key, ok, err=None, cost=1, kind="text", latency_ms=0.0):
        self.released.append({"ok": ok, "err": err, "kind": kind})
        return "ok" if ok else "unknown"


def _canned_json():
    clips = []
    cursor = 0
    for i in range(8):
        clips.append({"id": f"s{i + 1:02d}", "start": cursor, "duration": 8,
                      "narration": f"第{i + 1}镜旁白",
                      "image_prompt": "[主体]少年+[场景]雪夜+[风格]写实+[光照]月光+[构图]竖构图+[质量]1K,高细节",
                      "video_prompt": "[主体]少年+[动作]抬头+[场景]雪夜+[运镜]缓慢推镜+[光照]月光+[风格]写实",
                      "video_mode": "text"})
        cursor += 8
    return {"title": "剧", "total_seconds": 64, "aspect": "9:16",
            "resolution": "720x1280", "clip_seconds": "8",
            "character_refs": [], "clips": clips,
            "generated_by": "fake@test"}


def _client(monkeypatch):
    from fastapi.testclient import TestClient

    import main as m

    pool = _FakePool()
    monkeypatch.setattr(m, "_pool", lambda: pool)
    # 锁定文本配置，不随本机 models.yaml 漂移
    monkeypatch.setattr(m, "_text_cfg", lambda: {
        "provider": "agnes", "model": "agnes-3.0-flash", "base_url": "",
        "temperature": 0.7, "max_tokens": 8192, "timeout_s": 120,
        "fallback": {"mode": "manual"}})
    return TestClient(m.app), m, pool


def test_breakdown_api_ok(monkeypatch):
    c, m, pool = _client(monkeypatch)

    def _fake(prompt, **kw):
        assert kw.get("api_key") == "sk-fake"
        import json as _json
        return {"text": _json.dumps(_canned_json(), ensure_ascii=False),
                "generated_by": "fake/test@now"}

    monkeypatch.setattr(m, "_generate_text", _fake)
    r = c.post("/api/drama/breakdown", json={
        "name": "剧", "total_seconds": 64, "aspect": "9:16",
        "clip_seconds": "8", "style": "写实", "source_text": "少年雪夜下山" * 100})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and len(body["script"]["clips"]) == 8
    assert body["script"]["generated_by"] == "fake/test@now"
    assert pool.released and pool.released[-1]["ok"] is True


def test_breakdown_zen_skips_pool(monkeypatch):
    """zen 走 CLI：不断言池动作，只验 _generate_text 收到 zen 参数且免池可用。"""
    from fastapi.testclient import TestClient

    import main as m

    pool = _FakePool()
    monkeypatch.setattr(m, "_pool", lambda: pool)
    monkeypatch.setattr(m, "_text_cfg", lambda: {
        "provider": "opencode-zen", "model": "opencode/mimo-v2.5-free",
        "base_url": "", "temperature": 0.7, "max_tokens": 8192,
        "timeout_s": 120, "fallback": {"mode": "manual"}})

    def _fake(prompt, **kw):
        assert kw.get("provider") == "opencode-zen"
        assert kw.get("api_key", "") == ""
        return {"text": "OK",
                "generated_by": "opencode-zen/opencode/mimo-v2.5-free@now (cli)"}

    monkeypatch.setattr(m, "_generate_text", _fake)
    c = TestClient(m.app)
    r = c.post("/api/text/test", json={})
    assert r.status_code == 200, r.text
    assert r.json()["reply"] == "OK"
    assert pool.released == []  # CLI 通道不动密钥池


def test_breakdown_api_bad_json_is_502(monkeypatch):
    c, m, _pool = _client(monkeypatch)
    monkeypatch.setattr(m, "_generate_text",
                        lambda prompt, **kw: {"text": "我编不出来", "generated_by": "x"})
    r = c.post("/api/drama/breakdown", json={
        "name": "剧", "total_seconds": 60, "aspect": "9:16",
        "clip_seconds": "8", "source_text": "原文"})
    assert r.status_code == 502


def test_breakdown_api_validates_input():
    from fastapi.testclient import TestClient

    import main as m

    c = TestClient(m.app)
    base = {"name": "剧", "aspect": "9:16", "clip_seconds": "8", "source_text": "原文"}
    assert c.post("/api/drama/breakdown", json={**base, "total_seconds": 30}).status_code == 400
    assert c.post("/api/drama/breakdown", json={**base, "total_seconds": 60, "clip_seconds": "3"}).status_code == 400
    assert c.post("/api/drama/breakdown", json={**base, "total_seconds": 60, "source_text": "  "}).status_code == 400


def test_text_test_api_ok(monkeypatch):
    c, m, pool = _client(monkeypatch)
    monkeypatch.setattr(
        m, "_generate_text",
        lambda prompt, **kw: {"text": "OK", "generated_by": "fake/test@now"})
    r = c.post("/api/text/test", json={"provider": "agnes", "model": "agnes-3.0-flash"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["reply"] == "OK" and body["latency_ms"] >= 0
    assert pool.released and pool.released[-1]["ok"] is True


def test_state_endpoint_includes_script():
    """回归：/state 必须带 script，否则前端会用空架子覆盖本地草稿（丢数据）。"""
    import shutil

    from fastapi.testclient import TestClient

    import main as m

    c = TestClient(m.app)
    script = {"title": "STATESCRIPT", "total_seconds": 60, "aspect": "9:16",
              "resolution": "720x1280", "clip_seconds": "8", "character_refs": [],
              "clips": [{"id": "s01", "start": 0, "duration": 30, "narration": "a",
                         "image_prompt": "x", "video_prompt": "y", "video_mode": "text"},
                        {"id": "s02", "start": 30, "duration": 30, "narration": "b",
                         "image_prompt": "x", "video_prompt": "y", "video_mode": "text"}]}
    try:
        assert c.post("/api/drama/new", json={"name": "STATESCRIPT", "script": script}).status_code == 200
        body = c.get("/api/drama/STATESCRIPT/state").json()
        assert body["script"]["title"] == "STATESCRIPT"
        assert len(body["script"]["clips"]) == 2
        assert len(body["clips"]) == 2  # 队列兼容展平仍在
    finally:
        from pathlib import Path as _Path

        shutil.rmtree(_Path(m.__file__).resolve().parent.parent / "outputs" / "STATESCRIPT",
                      ignore_errors=True)


def test_text_test_api_requires_config():
    from fastapi.testclient import TestClient

    import main as m

    c = TestClient(m.app)
    monkeypatch_cfg = {"text": {"provider": "", "model": ""}}
    orig = m._local_models
    m._local_models = lambda: dict(orig()) | monkeypatch_cfg  # noqa: E731
    try:
        r = c.post("/api/text/test", json={})
        assert r.status_code == 400
    finally:
        m._local_models = orig


def _mem_pool():
    from keypool import CUSTOM_ENDPOINT_TAG, KeyPoolManager

    mgr = KeyPoolManager()
    agnes = mgr.register_key("sk-agnes-111", account_tag="acc-01")
    custom = mgr.register_key("sk-custom-999", account_tag=CUSTOM_ENDPOINT_TAG)
    return mgr, agnes, custom


def test_acquire_excludes_custom_for_agnes():
    mgr, agnes, custom = _mem_pool()
    got = mgr.acquire("text", exclude_tag="自建端点")
    assert got.raw_key == "sk-agnes-111"
    mgr.release(got, True, kind="text")


def test_acquire_require_custom():
    from keypool import PoolExhausted

    mgr, agnes, custom = _mem_pool()
    got = mgr.acquire("text", require_tag="自建端点")
    assert got.raw_key == "sk-custom-999"
    mgr.release(got, True, kind="text")
    mgr2 = __import__("keypool").KeyPoolManager()
    mgr2.register_key("sk-agnes-111", account_tag="acc-01")
    with pytest.raises(PoolExhausted):
        mgr2.acquire("text", require_tag="自建端点")


def test_custom_key_status():
    from keypool import KeyPoolManager

    mgr = KeyPoolManager()
    assert mgr.custom_endpoint_key_status() == {"configured": False, "mask": None}
    mgr.register_key("sk-custom-999", account_tag="自建端点")
    st = mgr.custom_endpoint_key_status()
    assert st["configured"] is True and st["mask"].startswith("sk-")


def test_custom_key_endpoint_and_status_api(monkeypatch):
    from fastapi.testclient import TestClient

    import main as m
    from keypool import KeyPoolManager

    mgr = KeyPoolManager()
    monkeypatch.setattr(m, "_pool", lambda: mgr)
    monkeypatch.setattr(m, "_text_cfg", lambda: {
        "provider": "openai-compatible", "model": "m", "base_url": "http://x/v1",
        "temperature": 0.7, "max_tokens": 512, "timeout_s": 30,
        "fallback": {"mode": "manual"}})
    c = TestClient(m.app)
    assert c.get("/api/text/status").json()["custom_key"] == {
        "configured": False, "mask": None}
    r = c.post("/api/text/custom-key", json={"raw_key": "sk-custom-999"})
    assert r.status_code == 200, r.text
    assert r.json()["mask"].startswith("sk-")
    st = c.get("/api/text/status").json()
    assert st["custom_key"]["configured"] is True
    assert c.post("/api/text/custom-key", json={"raw_key": "  "}).status_code == 400


def test_openai_compatible_uses_custom_key_only(monkeypatch):
    from fastapi.testclient import TestClient

    import main as m
    from keypool import KeyPoolManager

    mgr = KeyPoolManager()
    mgr.register_key("sk-agnes-111", account_tag="acc-01")
    mgr.register_key("sk-custom-999", account_tag="自建端点")
    monkeypatch.setattr(m, "_pool", lambda: mgr)
    monkeypatch.setattr(m, "_text_cfg", lambda: {
        "provider": "openai-compatible", "model": "m", "base_url": "http://x/v1",
        "temperature": 0.7, "max_tokens": 512, "timeout_s": 30,
        "fallback": {"mode": "manual"}})
    seen: dict = {}

    def _fake(prompt, **kw):
        seen.update(kw)
        return {"text": "OK", "generated_by": "x"}

    monkeypatch.setattr(m, "_generate_text", _fake)
    c = TestClient(m.app)
    r = c.post("/api/text/test", json={})
    assert r.status_code == 200, r.text
    assert seen.get("api_key") == "sk-custom-999"  # 绝不用 Agnes 池 Key


def test_openai_compatible_without_custom_key_400(monkeypatch):
    from fastapi.testclient import TestClient

    import main as m
    from keypool import KeyPoolManager

    mgr = KeyPoolManager()
    mgr.register_key("sk-agnes-111", account_tag="acc-01")
    monkeypatch.setattr(m, "_pool", lambda: mgr)
    monkeypatch.setattr(m, "_text_cfg", lambda: {
        "provider": "openai-compatible", "model": "m", "base_url": "http://x/v1",
        "temperature": 0.7, "max_tokens": 512, "timeout_s": 30,
        "fallback": {"mode": "manual"}})
    c = TestClient(m.app)
    r = c.post("/api/text/test", json={})
    assert r.status_code == 400
    assert "自建端点" in r.json()["detail"]
