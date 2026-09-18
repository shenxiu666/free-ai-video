"""合成滤镜测试（纯构造不断言执行；真执行走本地 ffmpeg 测试源，见下）。"""

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffmpeg_mux import build_mux_command, build_mux_filter  # noqa: E402


def _srt(tmp_path):
    p = tmp_path / "full.srt"
    p.write_text("1\n00:00:00,000 --> 00:00:02,000\n你好\n", encoding="utf-8")
    return p


def _filt(cmd):
    i = cmd.index("-filter_complex")
    return cmd[i + 1]


def _maps(cmd):
    out = []
    for i, v in enumerate(cmd):
        if v == "-map":
            out.append(cmd[i + 1])
    return out


def test_subtitles_on_video_chain_single(tmp_path):
    """回归：单镜字幕必须链在 [v0] 后，绝不缀音频链（曾报 pad 绑定失败）。"""
    srt = _srt(tmp_path)
    cmd = build_mux_command(["a.mp4"], srt, "out.mp4", [8.0])
    filt = _filt(cmd)
    assert "[v0]subtitles=" in filt
    assert ",subtitles" not in filt
    maps = _maps(cmd)
    assert maps[0] == "[vsub]" and maps[1] == "[aout]"


def test_subtitles_on_video_chain_xfade(tmp_path):
    srt = _srt(tmp_path)
    cmd = build_mux_command(["a.mp4", "b.mp4", "c.mp4"], srt, "out.mp4",
                            [8.0, 8.0, 8.0])
    filt = _filt(cmd)
    assert "[x2]subtitles=" in filt
    assert ",subtitles" not in filt
    assert _maps(cmd)[0] == "[vsub]"


def test_subtitles_on_video_chain_concat(tmp_path):
    srt = _srt(tmp_path)
    cmd = build_mux_command(["a.mp4", "b.mp4"], srt, "out.mp4", None)
    filt = _filt(cmd)
    assert "[vcat]subtitles=" in filt
    assert ",subtitles" not in filt
    assert _maps(cmd)[0] == "[vsub]"


def test_no_srt_no_subtitles():
    cmd = build_mux_command(["a.mp4"], None, "out.mp4", [8.0])
    assert "subtitles" not in _filt(cmd)
    assert _maps(cmd) == ["[v0]", "[aout]"]


def _wav(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"RIFF....WAVE")
    return p


def test_dub_mix_two_tracks(tmp_path):
    w1 = _wav(tmp_path, "s01.wav")
    w2 = _wav(tmp_path, "s02.wav")
    cmd = build_mux_command(["a.mp4", "b.mp4"], None, "out.mp4", [8.0, 8.0],
                            dub_tracks=[(str(w1), 0), (str(w2), 8.0)])
    # 输入：2 视频 + 2 配音
    assert cmd.count("-i") == 4
    filt = _filt(cmd)
    assert "adelay=0|0[dub0]" in filt
    assert "adelay=8000|8000[dub1]" in filt
    # 超长配音 bleed 不断尾：apad 补齐 + amix longest（2026-09-17 由 first 改 longest）
    assert "amix=inputs=3:duration=longest" in filt
    assert "[dub0]apad=whole_dur=15.700[dub0p]" in filt
    assert "[dub1]apad=whole_dur=15.700[dub1p]" in filt
    # loudnorm 在 amix 之后（作用于最终混音）
    assert filt.index("amix=") < filt.index("loudnorm")
    assert _maps(cmd) == ["[x1]", "[aout]"]


def test_missing_wav_skipped(tmp_path):
    w1 = _wav(tmp_path, "s01.wav")
    cmd = build_mux_command(["a.mp4", "b.mp4"], None, "out.mp4", [8.0, 8.0],
                            dub_tracks=[(str(w1), 0),
                                        (str(tmp_path / "nope.wav"), 8.0)])
    assert cmd.count("-i") == 3
    filt = _filt(cmd)
    assert "amix=inputs=2:duration=longest" in filt
    assert "dub1" not in filt


def test_no_dubs_shape_unchanged():
    filt, vout, aout = build_mux_filter(1, [8.0])
    assert (vout, aout) == ("[v0]", "[aout]")
    assert "[a0]loudnorm[aout]" in filt and "amix" not in filt


def test_silent_clip_gets_sized_nullsrc():
    """无音频源：anullsrc 必须带定长 d（无限静音源+amix longest 会让 ffmpeg 永不结束）。

    回归 2026-09-17 端到端挂 300s 超时。
    """
    filt, _, _ = build_mux_filter(2, [2.0, 2.0], audio_present=[False, True])
    assert "anullsrc=r=48000:cl=stereo:d=2.000[a0]" in filt
    assert "[1:a]aformat" in filt


def test_silent_clip_unknown_duration_fails_fast():
    """无音频又探不到时长：fail fast 抛错，绝不产出会挂死的命令。"""
    with pytest.raises(ValueError, match="无法补静音"):
        build_mux_filter(2, None, audio_present=[False, True])


def _stub_verify(monkeypatch, fail=False):
    """桩掉强校验：runner 内联 from ffmpeg_mux import verify_final，桩模块属性即生效。"""
    import ffmpeg_mux as F

    def _ok(out, expected_seconds=None, tolerance=1.0):
        return {"path": str(out), "size": 1, "duration": None,
                "has_video": True, "has_audio": True, "message": "stub 通过"}

    def _boom(out, expected_seconds=None, tolerance=1.0):
        raise RuntimeError("stub 强校验失败")

    monkeypatch.setattr(F, "verify_final", _boom if fail else _ok, raising=True)


def test_runner_passes_dub_tracks(monkeypatch, tmp_path):
    """runner 按 ready 镜组 dub_tracks（wav 路径 + 剧本 start）。

    缺 wav 的镜跳过但仍出片（警告+留痕，不阻塞），缺 srt 同理记入 mux_detail。
    """
    import json as _json

    import runner as R

    captured: dict = {}

    def _fake_mux(videos, srt, out, dry_run=False, timeout=600, dub_tracks=None):
        captured["dub_tracks"] = dub_tracks
        Path(str(out)).write_bytes(b"mp4")
        return str(out)

    marks: list = []
    logs: list = []
    _stub_verify(monkeypatch)
    monkeypatch.setattr(R, "_mux_clips", _fake_mux)
    monkeypatch.setattr(R, "_mark_stage",
                        lambda *a: marks.append((a[1], a[2], a[3])))
    (tmp_path / "videos").mkdir()
    (tmp_path / "videos" / "s01.mp4").write_bytes(b"v")
    (tmp_path / "videos" / "s02.mp4").write_bytes(b"v")
    (tmp_path / "audio").mkdir()
    (tmp_path / "audio" / "s01.wav").write_bytes(b"a")
    # s02 无 wav：dub 跳过（视频本身齐全，照常合成）
    w = R._Worker.__new__(R._Worker)
    w.name = "W"
    w.ctx = {}
    w.only = set()
    w.ddir = tmp_path
    w.script = {}
    w.log = lambda msg, *a, **k: logs.append(msg)
    clips = [{"id": "s01", "start": 0}, {"id": "s02", "start": 30}]
    w._run_mux(clips)
    assert captured["dub_tracks"] == [(str(tmp_path / "audio" / "s01.wav"), 0.0)]
    assert ("s01", "mux", "done") in marks and ("s02", "mux", "done") in marks
    detail = _json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["mux_detail"]
    assert detail["missing_dubs"] == ["s02"] and detail["dub_ok"] == 1 and detail["dub_total"] == 2
    assert any("缺配音" in m for m in logs)


def test_final_versioned_not_overwritten(monkeypatch, tmp_path):
    """回归：每次合成独立版本存成片/文件夹保留，final.mp4 恒为最新指针，state 留痕。"""
    import json as _json

    import runner as R

    outs: list = []

    def _fake_mux(videos, srt, out, dry_run=False, timeout=600, dub_tracks=None):
        outs.append(str(out))
        Path(str(out)).write_bytes(b"mp4")
        return str(out)

    marks: list = []
    _stub_verify(monkeypatch)
    monkeypatch.setattr(R, "_mux_clips", _fake_mux)
    monkeypatch.setattr(R, "_mark_stage",
                        lambda *a: marks.append((a[1], a[2], a[3])))
    (tmp_path / "videos").mkdir()
    (tmp_path / "videos" / "s01.mp4").write_bytes(b"v")
    (tmp_path / "state.json").write_text(
        _json.dumps({"drama": "W", "clips": {}}), encoding="utf-8")
    w = R._Worker.__new__(R._Worker)
    w.name = "W"
    w.ctx = {}
    w.only = set()
    w.ddir = tmp_path
    w.script = {}
    w.log = lambda *a, **k: None
    clips = [{"id": "s01", "start": 0}]
    w._run_mux(clips)
    first = sorted((tmp_path / "成片").glob("final_*.mp4"))
    assert len(first) == 1 and (tmp_path / "final.mp4").is_file()
    import time as _time

    _time.sleep(1.1)  # 跨秒，确保时间戳不同
    w._run_mux(clips)
    second = sorted((tmp_path / "成片").glob("final_*.mp4"))
    assert len(second) == 2 and second[0] != second[1]
    assert (tmp_path / "final.mp4").is_file()
    assert not list(tmp_path.glob("final_*.mp4"))  # 剧根目录不再堆版本文件
    finals = _json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["finals"]
    assert [f["file"] for f in finals] == [f"成片/{p.name}" for p in second]
    assert all("created_at" in f for f in finals)


def _mux_worker(monkeypatch, tmp_path, script_clips):
    import runner as R

    w = R._Worker.__new__(R._Worker)
    w.name = "W"
    w.ctx = {}
    w.only = set()
    w.ddir = tmp_path
    w.script = {"clips": script_clips}
    w.log = lambda *a, **k: None
    return w


def test_partial_full_run_produces_no_version(monkeypatch, tmp_path):
    """门禁：全剧跑但缺一镜视频 → 不出残片版本、不碰指针，缺的镜标 failed。"""
    import json as _json

    import runner as R

    calls: list = []
    monkeypatch.setattr(R, "_mux_clips", lambda *a, **k: calls.append((a, k)))
    marks: list = []
    logs: list = []
    monkeypatch.setattr(R, "_mark_stage",
                        lambda *a: marks.append((a[1], a[2], a[3])))
    (tmp_path / "videos").mkdir()
    (tmp_path / "videos" / "s01.mp4").write_bytes(b"v")
    (tmp_path / "state.json").write_text(
        _json.dumps({"drama": "W", "clips": {}}), encoding="utf-8")
    w = _mux_worker(monkeypatch, tmp_path,
                    [{"id": "s01", "start": 0}, {"id": "s02", "start": 8}])
    w.log = lambda msg, *a, **k: logs.append(msg)
    w._run_mux(w._full_clips())
    assert calls == []
    assert ("s02", "mux", "failed") in marks
    assert list(tmp_path.glob("成片/final_*.mp4")) == []
    assert not (tmp_path / "final.mp4").exists()
    assert "finals" not in _json.loads(
        (tmp_path / "state.json").read_text(encoding="utf-8"))
    assert any("门禁" in m for m in logs)


def test_zero_byte_video_treated_as_missing(monkeypatch, tmp_path):
    """0 字节残留文件不算对应视频。"""
    import runner as R

    calls: list = []
    monkeypatch.setattr(R, "_mux_clips", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(R, "_mark_stage", lambda *a: None)
    (tmp_path / "videos").mkdir()
    (tmp_path / "videos" / "s01.mp4").write_bytes(b"v")
    (tmp_path / "videos" / "s02.mp4").write_bytes(b"")
    w = _mux_worker(monkeypatch, tmp_path,
                    [{"id": "s01", "start": 0}, {"id": "s02", "start": 8}])
    w.log = lambda *a, **k: None
    w._run_mux(w._full_clips())
    assert calls == []


def test_scoped_retry_never_muxes(monkeypatch, tmp_path):
    """单镜重试未齐：不得合成残片、不得标 mux done、不得碰指针、不得留痕。"""
    import runner as R

    calls: list = []

    def _fake_mux(*a, **k):
        calls.append((a, k))
        return "x"

    marks: list = []
    logs: list = []
    monkeypatch.setattr(R, "_mux_clips", _fake_mux)
    monkeypatch.setattr(R, "_mark_stage",
                        lambda *a: marks.append((a[1], a[2], a[3])))
    (tmp_path / "videos").mkdir()
    (tmp_path / "videos" / "s03.mp4").write_bytes(b"v")
    pointer = tmp_path / "final.mp4"
    pointer.write_bytes(b"old-full-cut")
    w = R._Worker.__new__(R._Worker)
    w.name = "W"
    w.ctx = {}
    w.only = {"s03"}
    w.ddir = tmp_path
    w.script = {"clips": [{"id": "s03", "start": 16}, {"id": "s04", "start": 24}]}
    w.log = lambda msg, *a, **k: logs.append(msg)
    w._run_mux([{"id": "s03", "start": 16}])
    assert calls == []
    # 没真合成就不标 done：mux 状态保持不动（此前误标 done 已修复）
    assert not [m for m in marks if m[1] == "mux"]
    assert list(tmp_path.glob("成片/final_*.mp4")) == []
    assert pointer.read_bytes() == b"old-full-cut"
    assert any("开始渲染" in m for m in logs)


def test_scoped_retry_muxes_when_drama_complete(monkeypatch, tmp_path):
    """单镜补齐最后一块 → 自动出完整版（只此一种情况单镜触发合成）。"""
    import json as _json

    import runner as R

    outs: list = []

    def _fake_mux(videos, srt, out, dry_run=False, timeout=600, dub_tracks=None):
        outs.append(str(out))
        Path(str(out)).write_bytes(b"mp4")
        return str(out)

    marks: list = []
    logs: list = []
    _stub_verify(monkeypatch)
    monkeypatch.setattr(R, "_mux_clips", _fake_mux)
    monkeypatch.setattr(R, "_mark_stage",
                        lambda *a: marks.append((a[1], a[2], a[3])))
    (tmp_path / "videos").mkdir()
    (tmp_path / "videos" / "s03.mp4").write_bytes(b"v")
    (tmp_path / "videos" / "s04.mp4").write_bytes(b"v")
    (tmp_path / "state.json").write_text(
        _json.dumps({"drama": "W", "clips": {}}), encoding="utf-8")
    w = R._Worker.__new__(R._Worker)
    w.name = "W"
    w.ctx = {}
    w.only = {"s04"}
    w.ddir = tmp_path
    w.script = {"clips": [{"id": "s03", "start": 16}, {"id": "s04", "start": 24}]}
    w.log = lambda msg, *a, **k: logs.append(msg)
    w._run_mux([{"id": "s04", "start": 24}])
    vers = sorted((tmp_path / "成片").glob("final_*.mp4"))
    assert len(vers) == 1 and len(outs) == 1
    assert (tmp_path / "final.mp4").is_file()
    assert ("s03", "mux", "done") in marks and ("s04", "mux", "done") in marks
    assert any("补齐最后一块" in m for m in logs)


def test_verify_failure_marks_failed_no_pointer(monkeypatch, tmp_path):
    """强校验失败：ready 全标 failed、不碰指针、不记 finals、删残版。"""
    import json as _json

    import runner as R

    def _fake_mux(videos, srt, out, dry_run=False, timeout=600, dub_tracks=None):
        Path(str(out)).write_bytes(b"mp4")
        return str(out)

    marks: list = []
    _stub_verify(monkeypatch, fail=True)
    monkeypatch.setattr(R, "_mux_clips", _fake_mux)
    monkeypatch.setattr(R, "_mark_stage",
                        lambda *a: marks.append((a[1], a[2], a[3])))
    (tmp_path / "videos").mkdir()
    (tmp_path / "videos" / "s01.mp4").write_bytes(b"v")
    (tmp_path / "state.json").write_text(
        _json.dumps({"drama": "W", "clips": {}}), encoding="utf-8")
    pointer = tmp_path / "final.mp4"
    pointer.write_bytes(b"old")
    w = R._Worker.__new__(R._Worker)
    w.name = "W"
    w.ctx = {}
    w.only = set()
    w.ddir = tmp_path
    w.script = {}
    w.log = lambda *a, **k: None
    with pytest.raises(RuntimeError, match="合成失败"):
        w._run_mux([{"id": "s01", "start": 0, "duration": 8}])
    assert ("s01", "mux", "failed") in marks
    assert pointer.read_bytes() == b"old"
    assert list((tmp_path / "成片").glob("final_*.mp4")) == []
    assert "finals" not in _json.loads(
        (tmp_path / "state.json").read_text(encoding="utf-8"))


def test_verify_final_missing_and_empty(tmp_path):
    """verify_final：缺失与 0 字节残留直接 raise（无需 ffprobe）。"""
    import ffmpeg_mux as F

    with pytest.raises(RuntimeError, match="缺失"):
        F.verify_final(tmp_path / "no.mp4")
    e = tmp_path / "empty.mp4"
    e.write_bytes(b"")
    with pytest.raises(RuntimeError, match="为空"):
        F.verify_final(e)


def _ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def test_verify_final_real_streams_and_duration(tmp_path):
    """verify_final 真文件：音视频流通过、时长超差 raise、纯视频缺 audio raise。"""
    import subprocess

    import ffmpeg_mux as F

    if not _ffmpeg_available():
        pytest.skip("缺 ffmpeg/ffprobe")
    av = tmp_path / "av.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=128x128:rate=10:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(av)],
        capture_output=True, timeout=120, check=True)
    got = F.verify_final(av, expected_seconds=2.0)
    assert got["has_video"] and got["has_audio"]
    with pytest.raises(RuntimeError, match="误差"):
        F.verify_final(av, expected_seconds=10.0)
    vo = tmp_path / "vo.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=128x128:rate=10:duration=1",
         "-pix_fmt", "yuv420p", "-an", str(vo)],
        capture_output=True, timeout=120, check=True)
    with pytest.raises(RuntimeError, match="缺 audio"):
        F.verify_final(vo)


def test_mux_clips_e2e_silent_sources_plus_dub(tmp_path):
    """端到端：无音频源视频（anullsrc 补静音）+ 配音混入 → 有音视频流的成片。

    回归“语言和视频没有被剪一起”：dub 必须进 amix longest 链并产出带音频的 final。
    """
    import subprocess

    import ffmpeg_mux as F

    if not _ffmpeg_available():
        pytest.skip("缺 ffmpeg/ffprobe")
    clips = []
    for i in range(2):
        c = tmp_path / f"c{i}.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=320x640:rate=10:duration=2",
             "-pix_fmt", "yuv420p", "-an", str(c)],
            capture_output=True, timeout=120, check=True)
        clips.append(c)
    dub = tmp_path / "dub.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
         "-c:a", "pcm_s16le", str(dub)],
        capture_output=True, timeout=120, check=True)
    out = tmp_path / "final.mp4"
    got = F.mux_clips(clips, None, out, timeout=300,
                      dub_tracks=[(dub, 0.5)])
    assert Path(got).is_file()
    # 2+2s 减 0.3 xfade ≈ 3.7s，容差 ±1s（04.9 验收）
    checked = F.verify_final(out, expected_seconds=3.7)
    assert checked["has_video"] and checked["has_audio"]


def _clip_worker(tmp_path, **kw):
    import runner as R

    w = R._Worker.__new__(R._Worker)
    w.name = "W"
    w.ctx = {}
    w.only = set()
    w.ddir = tmp_path
    w.script = {}
    w.log = lambda *a, **k: None
    for k, v in kw.items():
        setattr(w, k, v)
    return w


def test_clip_mux_success_marks_done(monkeypatch, tmp_path):
    """单镜合成成功：产出 final_clips/s01.mp4 并标 done（视频+配音+字幕三合一）。"""
    import runner as R

    seen: dict = {}

    def _fake_mux(clips, srt, out, dry_run=False, timeout=600, dub_tracks=None):
        seen["clips"] = clips
        seen["srt"] = srt
        seen["dub_tracks"] = dub_tracks
        Path(str(out)).parent.mkdir(parents=True, exist_ok=True)
        Path(str(out)).write_bytes(b"mp4")
        return str(out)

    marks: list = []
    monkeypatch.setattr(R, "_mux_clips", _fake_mux)
    monkeypatch.setattr(R, "_mark_stage",
                        lambda *a: marks.append((a[1], a[2], a[3])))
    (tmp_path / "videos").mkdir()
    (tmp_path / "videos" / "s01.mp4").write_bytes(b"v")
    (tmp_path / "audio").mkdir()
    (tmp_path / "audio" / "s01.wav").write_bytes(b"a")
    (tmp_path / "audio" / "s01.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n词\n",
                                               encoding="utf-8")
    w = _clip_worker(tmp_path)
    w._run_clip_mux({"id": "s01"})
    assert (tmp_path / "final_clips" / "s01.mp4").is_file()
    assert ("s01", "mux", "done") in marks
    assert seen["dub_tracks"] == [(str(tmp_path / "audio" / "s01.wav"), 0.0)]
    assert seen["srt"] == str(tmp_path / "audio" / "s01.srt")


def test_clip_mux_no_artifact_never_done(monkeypatch, tmp_path):
    """硬规则：mux 返回了但没成品文件 → 标 failed，绝不标 done。"""
    import runner as R

    def _liar_mux(clips, srt, out, dry_run=False, timeout=600, dub_tracks=None):
        return str(out)  # 号称成功，实则没写文件

    marks: list = []
    monkeypatch.setattr(R, "_mux_clips", _liar_mux)
    monkeypatch.setattr(R, "_mark_stage",
                        lambda *a: marks.append((a[1], a[2], a[3])))
    (tmp_path / "videos").mkdir()
    (tmp_path / "videos" / "s01.mp4").write_bytes(b"v")
    w = _clip_worker(tmp_path)
    with pytest.raises(RuntimeError, match="无成品文件"):
        w._run_clip_mux({"id": "s01"})
    assert ("s01", "mux", "failed") in marks
    assert not [m for m in marks if m == ("s01", "mux", "done")]


def test_clip_mux_missing_video_fails_without_muxing(monkeypatch, tmp_path):
    """缺分镜视频：不动 ffmpeg，直接 failed。"""
    import runner as R

    calls: list = []
    monkeypatch.setattr(R, "_mux_clips", lambda *a, **k: calls.append((a, k)))
    marks: list = []
    monkeypatch.setattr(R, "_mark_stage",
                        lambda *a: marks.append((a[1], a[2], a[3])))
    (tmp_path / "videos").mkdir()
    w = _clip_worker(tmp_path)
    with pytest.raises(RuntimeError, match="缺分镜视频"):
        w._run_clip_mux({"id": "s07"})
    assert calls == []
    assert ("s07", "mux", "failed") in marks


def test_clip_mux_missing_dub_warns_but_produces(monkeypatch, tmp_path):
    """缺配音：warn 点名仍出成品（dub_tracks=None），标 done。"""
    import runner as R

    seen: dict = {}

    def _fake_mux(clips, srt, out, dry_run=False, timeout=600, dub_tracks=None):
        seen["dub_tracks"] = dub_tracks
        Path(str(out)).parent.mkdir(parents=True, exist_ok=True)
        Path(str(out)).write_bytes(b"mp4")
        return str(out)

    marks: list = []
    logs: list = []
    monkeypatch.setattr(R, "_mux_clips", _fake_mux)
    monkeypatch.setattr(R, "_mark_stage",
                        lambda *a: marks.append((a[1], a[2], a[3])))
    (tmp_path / "videos").mkdir()
    (tmp_path / "videos" / "s01.mp4").write_bytes(b"v")
    w = _clip_worker(tmp_path)
    w.log = lambda msg, *a, **k: logs.append(msg)
    w._run_clip_mux({"id": "s01"})
    assert seen["dub_tracks"] is None
    assert ("s01", "mux", "done") in marks
    assert any("缺配音" in m for m in logs)


def test_clip_mux_skipped_when_final_exists(monkeypatch, tmp_path):
    """成品已存在且 state done：直接跳过，不碰 ffmpeg、不改状态。"""
    import runner as R

    calls: list = []
    monkeypatch.setattr(R, "_mux_clips", lambda *a, **k: calls.append((a, k)))
    marks: list = []
    monkeypatch.setattr(R, "_mark_stage",
                        lambda *a: marks.append((a[1], a[2], a[3])))
    monkeypatch.setattr(R, "_is_stage_done", lambda *a, **k: True)
    (tmp_path / "final_clips").mkdir(parents=True)
    (tmp_path / "final_clips" / "s01.mp4").write_bytes(b"mp4")
    w = _clip_worker(tmp_path)
    w._run_clip_mux({"id": "s01"})
    assert calls == []
    assert marks == []


def test_empty_dub_preserves_native_audio(tmp_path):
    """B路线：对白镜传空dub即保留原音（无amix、直走loudnorm），不断言失败。"""
    # filter 层：None/[] 均走无混音旧形状
    for dubs in (None, []):
        filt, vout, aout = build_mux_filter(2, [8.0, 8.0], dubs=dubs)
        assert "amix" not in filt
        assert "[a0]loudnorm[aout]" not in filt  # xfade 路 a_base 为 [m1]
        assert "loudnorm[aout]" in filt
        assert (vout, aout) == ("[x1]", "[aout]")
    # command 层：dub_tracks=None/[] 均不追加输入、不断言失败
    for tracks in (None, []):
        cmd = build_mux_command(["a.mp4", "b.mp4"], None, "out.mp4",
                                [8.0, 8.0], dub_tracks=tracks)
        assert cmd.count("-i") == 2
        filt = _filt(cmd)
        assert "amix" not in filt and "dub0" not in filt
        assert "loudnorm" in filt
        assert _maps(cmd) == ["[x1]", "[aout]"]
    # 全缺 wav 自动跳过同样保留原音形状
    cmd = build_mux_command(["a.mp4", "b.mp4"], None, "out.mp4", [8.0, 8.0],
                            dub_tracks=[(str(tmp_path / "nope.wav"), 0)])
    assert cmd.count("-i") == 2
    assert "amix" not in _filt(cmd)


def test_ssml_preserves_plain_text_path():
    """SSML轻增强不破坏纯文本路径：去标签还原原文，标点break可逆。"""
    import re

    from tts import build_narration_ssml

    def _strip(ssml: str) -> str:
        return re.sub(r"<[^>]+>", "", ssml)

    plain = "你好世界今天天气不错"
    ssml = build_narration_ssml(plain)
    assert plain in _strip(ssml)
    assert "<speak" in ssml and "<prosody" in ssml
    # ，。→500ms，！？→800ms，且原文标点保留、去标签可逆
    punct = "你好，今天不错。你确定吗？太棒了！"
    ssml2 = build_narration_ssml(punct)
    assert ssml2.count('break time="500ms"') == 2
    assert ssml2.count('break time="800ms"') == 2
    assert _strip(ssml2) == punct
    # emotion 可选参：默认与显式均不破坏原文，仅调 rate/pitch
    assert plain in _strip(build_narration_ssml(plain, emotion=""))
    calm = build_narration_ssml(plain, emotion="calm")
    assert plain in _strip(calm) and 'rate="-5%"' in calm
    # XML 转义不破坏结构
    esc = build_narration_ssml("A<B & C>")
    assert "&lt;" in esc and "&amp;" in esc
