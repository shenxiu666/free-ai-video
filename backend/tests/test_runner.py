"""渲染引擎测试（全桩：不调真模型、不耗 Key、不跑真 ffmpeg）。"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import runner as R  # noqa: E402


class FakePool:
    def __init__(self):
        self.released = []

    def acquire(self, kind, **kw):
        return types.SimpleNamespace(id="k1", raw_key="sk-fake")

    def release(self, key, ok, err=None, cost=1, kind="text", latency_ms=0.0):
        self.released.append({"ok": ok, "kind": kind, "cost": cost})

    def has_live_key(self):
        return True
        return "ok"


def test_shift_srt_time():
    assert R.shift_srt_time("00:00:01,500 --> 00:00:03,000", 8) == \
        "00:00:09,500 --> 00:00:11,000"
    assert R.shift_srt_time("plain text", 8) == "plain text"


def test_build_err_http():
    import httpx

    req = httpx.Request("POST", "http://x/")
    resp = httpx.Response(429, headers={"retry-after": "5"},
                          request=req, text="slow")
    err = R.build_err(httpx.HTTPStatusError("r", request=req, response=resp))
    assert err == {"status": 429, "body": "slow", "retry_after": "5"}
    assert R.build_err(ValueError("x"))["status"] == 0


def _stub_providers(monkeypatch):
    from pathlib import Path as _P

    def _img(prompt, size="1K", ratio="9:16", api_key="", timeout=120, client=None):
        assert api_key == "sk-fake"
        return {"url": "http://cdn/x.png", "generated_by": "t"}

    def _vid_submit(prompt, seconds, api_key, mode="text", size="720P",
                    first_frame=None, last_frame=None, images=None, audios=None,
                    timeout=60, client=None):
        assert api_key == "sk-fake"
        return "vid-1"

    def _vid_poll(video_id, api_key, timeout_total=1800, interval=3.0,
                  timeout=30, client=None, on_throttle=None):
        assert video_id == "vid-1" and api_key == "sk-fake"
        return {"video_url": "http://cdn/x.mp4", "generated_by": "t"}

    def _tts(provider, voice, text, srt_path=None, wav_path=None):
        _P(str(wav_path)).parent.mkdir(parents=True, exist_ok=True)
        _P(str(wav_path)).write_bytes(b"wav")
        _P(str(srt_path)).write_text(
            "1\n00:00:00,000 --> 00:00:02,000\n" + text + "\n", encoding="utf-8")
        return str(wav_path), str(srt_path)

    def _mux(clips, srt, out, dry_run=False, timeout=600, dub_tracks=None):
        _P(str(out)).parent.mkdir(parents=True, exist_ok=True)
        _P(str(out)).write_bytes(b"mp4")
        return str(out)

    def _verify_ok(out, expected_seconds=None, tolerance=1.0):
        # 强校验桩：_mux 桩只写垃圾字节，真 ffprobe 必拦；此处只验接线，不验文件
        return {"path": str(out), "size": 3, "duration": None,
                "has_video": True, "has_audio": True, "message": "stub 通过"}

    def _dl(self, url, dest):
        _P(str(dest)).parent.mkdir(parents=True, exist_ok=True)
        _P(str(dest)).write_bytes(b"bin")

    monkeypatch.setattr(R, "_generate_image", _img)
    monkeypatch.setattr(R, "_submit_video", _vid_submit)
    monkeypatch.setattr(R, "_poll_video", _vid_poll)
    monkeypatch.setattr(R, "_synthesize", _tts)
    monkeypatch.setattr(R, "_mux_clips", _mux)
    import ffmpeg_mux as _F

    monkeypatch.setattr(_F, "verify_final", _verify_ok, raising=True)
    monkeypatch.setattr(R._Worker, "_download", _dl)


def _make_drama(name="RUNNER"):
    from fastapi.testclient import TestClient

    import main as m

    c = TestClient(m.app)
    script = {"title": name, "total_seconds": 60, "aspect": "9:16",
              "resolution": "720x1280", "clip_seconds": "8", "character_refs": [],
              "clips": [{"id": "s01", "start": 0, "duration": 30, "narration": "甲",
                         "image_prompt": "p1", "video_prompt": "v1", "video_mode": "text"},
                        {"id": "s02", "start": 30, "duration": 30, "narration": "乙",
                         "image_prompt": "p2", "video_prompt": "v2", "video_mode": "text"}]}
    assert c.post("/api/drama/new", json={"name": name, "script": script}).status_code == 200
    return c


def test_worker_runs_all_stages(monkeypatch):
    import shutil

    import main as m

    _stub_providers(monkeypatch)
    _no_sleep(monkeypatch)
    pool = FakePool()
    c = _make_drama()
    try:
        R._run_drama("RUNNER", {"pool": pool, "image": {"size": "1K", "ratio": "9:16"},
                                "video": {}, "tts": {"provider": "edge-tts",
                                                     "voice": "zh-CN-XiaoxiaoNeural"}}, [])
        st = c.get("/api/drama/RUNNER/state").json()
        clips = st["state"]["clips"]
        for cid in ("s01", "s02"):
            assert clips[cid] == {"image": "done", "video": "done",
                                  "tts": "done", "mux": "done"}, clips[cid]
        root = Path(m.__file__).resolve().parent.parent / "outputs" / "RUNNER"
        assert (root / "final.mp4").is_file()
        assert "开始渲染" in (root / "render.log").read_text(encoding="utf-8")
        kinds = sorted(r["kind"] for r in pool.released)
        assert kinds == ["image", "image", "video", "video"]
    finally:
        import shutil as _sh

        _sh.rmtree(Path(m.__file__).resolve().parent.parent / "outputs" / "RUNNER",
                   ignore_errors=True)


def test_start_rejects_when_running_and_retry_kicks(monkeypatch):
    import main as m
    from fastapi.testclient import TestClient

    calls: dict = {}

    class _StubRunner:
        running = False

        def is_running(self, name):
            return self.running

        def start_render(self, name, ctx, only_clips=None):
            calls["name"] = name
            calls["only"] = only_clips
            self.running = True
            return True

    monkeypatch.setattr(m, "_runner", _StubRunner())
    monkeypatch.setattr(m, "_pool", lambda: FakePool())
    c = _make_drama("RUNNER2")
    try:
        r = c.post("/api/drama/RUNNER2/start", json={})
        assert r.status_code == 200, r.text
        assert calls == {"name": "RUNNER2", "only": []}
        assert c.post("/api/drama/RUNNER2/start", json={}).status_code == 409
        assert c.get("/api/drama/RUNNER2/render-status").json()["running"] is True
        m._runner.running = False
        r2 = c.post("/api/drama/RUNNER2/retry", json={"clip_id": "s01"})
        assert r2.status_code == 200
        assert r2.json()["render_started"] is True
        assert calls["only"] == ["s01"]
    finally:
        import shutil as _sh

        _sh.rmtree(Path(m.__file__).resolve().parent.parent / "outputs" / "RUNNER2",
                   ignore_errors=True)


def _no_sleep(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(R.time, "sleep", lambda s: sleeps.append(float(s)))
    return sleeps


def _worker_for(monkeypatch, clips):
    """直构造 worker（剧本桩，不碰磁盘 script.json 之外；日志静默）。"""
    import main as m

    root = Path(m.__file__).resolve().parent.parent / "outputs" / "W"
    w = R._Worker.__new__(R._Worker)
    w.name = "W"
    w.ctx = {"pool": FakePool()}
    w.pool = w.ctx["pool"]
    w.only = set()
    w.ddir = root
    w.script = {"character_refs": [], "clips": clips}
    w.log = lambda *a, **k: None
    return w


def _clip(cid="s01", **kw):
    base = {"id": cid, "start": 0, "duration": 8, "narration": "词",
            "image_prompt": "p", "video_prompt": "v", "video_mode": "text"}
    base.update(kw)
    return base


def test_poll_429_backs_off_and_continues(monkeypatch):
    """复刻第一集 s01：轮询 429 带 Retry-After → 退避续询同一单，不断任务。"""
    import httpx

    import providers.agnes_video as av

    calls = {"n": 0}
    sleeps = _no_sleep(monkeypatch)

    def _fake_get(url, params=None, headers=None):
        calls["n"] += 1
        req = httpx.Request("GET", "http://x/")
        if calls["n"] <= 2:
            resp = httpx.Response(429, headers={"retry-after": "2"}, request=req)
            raise httpx.HTTPStatusError("r", request=req, response=resp)
        return httpx.Response(200, json={"status": "completed",
                                         "data": {"video_url": "http://c/x.mp4"}},
                              request=req)

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def get(self, *a, **k):
            return _fake_get(*a, **k)

        def close(self):
            pass

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    seen: list = []
    out = av.poll_video("vid-9", "sk-x", timeout_total=600, interval=3.0,
                        on_throttle=lambda w, n: seen.append((w, n)))
    assert out["video_url"] == "http://c/x.mp4"
    assert [n for _, n in seen] == [1, 2]
    assert seen[0][0] == pytest.approx(3.0)  # Retry-After 2 +1
    assert sum(sleeps) >= 6.0  # 确实睡了而不是瞬间失败


def test_acquire_fail_marks_failed_not_doing(monkeypatch):
    """复刻 s02/s05/s07/s08：拿不到 Key 直接 failed，绝不停在 doing。"""
    from keypool import PoolExhausted

    marks: list = []
    monkeypatch.setattr(R._Worker, "_need", lambda self, *a: True)
    monkeypatch.setattr(R, "_mark_stage",
                        lambda *a: marks.append((a[1], a[2], a[3])))

    class _DeadPool(FakePool):
        def has_live_key(self):
            return False

        def acquire(self, kind, **kw):
            raise PoolExhausted("无可用", retry_after=None)

    w = _worker_for(monkeypatch, [_clip()])
    w.pool = _DeadPool()
    w.ctx = {"pool": w.pool}
    with pytest.raises(RuntimeError, match="池全灭"):
        w._run_video(_clip(), [_clip()], {})
    assert marks == [("s01", "video", "failed")]


def test_bucket_empty_waits_then_submits(monkeypatch):
    """复刻 s08（差 2 秒）：桶空但有活 Key → 等 pacing 重试，不判死刑。"""
    from keypool import PoolExhausted

    _no_sleep(monkeypatch)
    attempts = {"n": 0}

    class _ThinPool(FakePool):
        def acquire(self, kind, **kw):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise PoolExhausted("空", retry_after=None)
            return types.SimpleNamespace(id="k1", raw_key="sk-fake")

    marks: list = []
    monkeypatch.setattr(R._Worker, "_need", lambda self, *a: True)
    monkeypatch.setattr(R, "_mark_stage",
                        lambda *a: marks.append((a[1], a[2], a[3])))
    monkeypatch.setattr(R, "_submit_video", lambda *a, **k: "vid-1")
    monkeypatch.setattr(R, "_poll_video",
                        lambda *a, **k: {"video_url": "http://c/x.mp4"})

    def _dl(self, url, dest):
        Path(str(dest)).parent.mkdir(parents=True, exist_ok=True)
        Path(str(dest)).write_bytes(b"bin")

    monkeypatch.setattr(R._Worker, "_download", _dl)
    w = _worker_for(monkeypatch, [_clip()])
    w.pool = _ThinPool()
    w.ctx = {"pool": w.pool}
    try:
        w._run_video(_clip(), [_clip()], {})
    finally:
        import shutil as _sh

        _sh.rmtree(w.ddir, ignore_errors=True)
    assert attempts["n"] == 3
    assert ("s01", "video", "doing") in marks
    assert marks[-1] == ("s01", "video", "done")


def test_submit_429_retries_same_clip(monkeypatch):
    """提交 429 重提同一镜（最多 3 次），换 Key 不泄漏。"""
    import httpx

    _no_sleep(monkeypatch)
    submits = {"n": 0}
    released_ids: list = []

    def _boom(*a, **k):
        submits["n"] += 1
        if submits["n"] < 3:
            req = httpx.Request("POST", "http://x/")
            resp = httpx.Response(429, request=req, text="busy")
            raise httpx.HTTPStatusError("r", request=req, response=resp)
        return "vid-7"

    class _Pool(FakePool):
        def release(self, key, ok, err=None, cost=1, kind="text", latency_ms=0.0):
            released_ids.append(getattr(key, "id", "?"))
            return "A"

    marks: list = []
    monkeypatch.setattr(R._Worker, "_need", lambda self, *a: True)
    monkeypatch.setattr(R, "_mark_stage",
                        lambda *a: marks.append((a[1], a[2], a[3])))
    monkeypatch.setattr(R, "_submit_video", _boom)
    monkeypatch.setattr(R, "_poll_video",
                        lambda *a, **k: {"video_url": "http://c/x.mp4"})

    def _dl(self, url, dest):
        Path(str(dest)).parent.mkdir(parents=True, exist_ok=True)
        Path(str(dest)).write_bytes(b"bin")

    monkeypatch.setattr(R._Worker, "_download", _dl)
    w = _worker_for(monkeypatch, [_clip()])
    w.pool = _Pool()
    w.ctx = {"pool": w.pool}
    w._pace_video = lambda: None
    vid, entry, key = w._submit_with_retry(
        "s01", _clip(), "8", "sk-fake", "text", None, [], [], w.pool.acquire("video"), 0.0)
    assert (vid, submits["n"]) == ("vid-7", 3)
    assert released_ids == ["k1", "k1"]  # 前两次失败各归还一次，无泄漏无 double-release


def test_mux_done_without_clip_final_repaired_on_read():
    """老任务谎话收敛：mux done 但无单镜成品 → 读 state 时降为 failed 并落盘。

    有成品的 done 不动；重复读保持稳定（纠一次）。
    """
    import json as _json

    import main as m

    c = _make_drama("REPAIR1")
    root = Path(m.__file__).resolve().parent.parent / "outputs" / "REPAIR1"
    try:
        sp = root / "state.json"
        st = _json.loads(sp.read_text(encoding="utf-8"))
        st["clips"]["s01"]["mux"] = "done"  # 老版本谎话：无 final_clips/s01.mp4
        sp.write_text(_json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        assert not (root / "final_clips" / "s01.mp4").exists()

        r = c.get("/api/drama/REPAIR1/state")
        assert r.status_code == 200, r.text
        clips = {x["id"]: x for x in r.json()["clips"]}
        assert clips["s01"]["mux"] == "failed"
        # 已落盘：谎话只纠一次
        st2 = _json.loads(sp.read_text(encoding="utf-8"))
        assert st2["clips"]["s01"]["mux"] == "failed"
        r2 = c.get("/api/drama/REPAIR1/state")
        assert r2.status_code == 200
        assert {x["id"]: x for x in r2.json()["clips"]}["s01"]["mux"] == "failed"

        # 对照组：有成品的 done 不动
        (root / "final_clips").mkdir(parents=True, exist_ok=True)
        (root / "final_clips" / "s02.mp4").write_bytes(b"mp4")
        st2["clips"]["s02"]["mux"] = "done"
        sp.write_text(_json.dumps(st2, ensure_ascii=False, indent=2), encoding="utf-8")
        r3 = c.get("/api/drama/REPAIR1/state")
        assert r3.status_code == 200, r3.text
        assert {x["id"]: x for x in r3.json()["clips"]}["s02"]["mux"] == "done"
    finally:
        import shutil as _sh

        _sh.rmtree(root, ignore_errors=True)
