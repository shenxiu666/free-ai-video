"""FFmpeg 合成链（docs/04.7、04.8）：

concat(硬切拼接) → 每接缝 xfade duration=0.3 → 配音按镜对齐混入(amix)
→ loudnorm(响度归一) → subtitles 烧录 srt，输出 720x1280 final.mp4。

失败语义：ffmpeg 非 0 即抛 RuntimeError，不重跑生成，
仅修 srt/路径后重调 mux_clips。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Sequence

WIDTH, HEIGHT = 720, 1280
XFADE_DURATION = 0.3


def probe_duration(path: str | Path) -> Optional[float]:
    """用 ffprobe 探测时长；无 ffprobe/失败返回 None（调用方降级 concat）。"""
    if shutil.which("ffprobe") is None:
        return None
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return None
        dur = json.loads(proc.stdout).get("format", {}).get("duration")
        return float(dur) if dur else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def has_audio_stream(path: str | Path) -> bool:
    """该视频是否有音频流；无 ffprobe/探测失败返回 True（假设有音频，保持旧行为）。

    用 ffprobe show_streams 查 audio，避免无音频输入在 [i:a] 处直接报错；
    调用方对 False 的路用 anullsrc 补静音。
    """
    if shutil.which("ffprobe") is None:
        return True
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "stream=codec_type",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            return True  # 文件缺失/不可探时假设有音频，保持旧形状
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams", []) or []
        return any(s.get("codec_type") == "audio" for s in streams)
    except (OSError, ValueError, subprocess.SubprocessError):
        return True


def build_mux_filter(
    n: int, durations: Optional[Sequence[Optional[float]]] = None,
    dubs: Optional[Sequence[tuple[int, int]]] = None,
    audio_present: Optional[Sequence[bool]] = None,
) -> tuple[str, str, str]:
    """构造 filter_complex，返回 (filter, vout_label, aout_label)。

    有全量时长 → xfade 链（每缝 0.3s）+ acrossfade；
    否则降级 concat（仍保证总时长=剧本时长）。
    配音（dubs=[(输入序号, 延迟毫秒)]）经 adelay 对齐后 apad 保 bleed，
    再与原音频 amix(duration=longest 不截超长配音尾)，最后统一 loudnorm。
    无 dubs 时音频直走 loudnorm，与旧形状一致。
    audio_present：各视频输入是否有音频流（None=全 True 兼容旧单测）；
    缺音频的路用 anullsrc 补静音，保证 [a{i}] 可参与 concat/acrossfade/amix。
    """
    if n < 1:
        raise ValueError("clips 不能为空")
    # audio_present 归一化：None 全 True；长度不齐补 True（保持旧行为）
    if audio_present is None:
        ap = [True] * n
    else:
        ap = list(audio_present)
        if len(ap) < n:
            ap = ap + [True] * (n - len(ap))
        else:
            ap = ap[:n]
    parts: list[str] = []
    for i in range(n):
        parts.append(
            f"[{i}:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},setsar=1,fps=30[v{i}]"
        )
        if not ap[i]:
            # 无音频流输入：anullsrc 补定长静音（d 必须取该镜探得时长；
            # anullsrc 缺 d 即无限长，会与 amix longest 叠加让 ffmpeg 永不结束）。
            # 时长未知则直接抛错 fail fast，绝不挂死等待。
            dur_i: Optional[float] = None
            if durations and len(durations) == n:
                try:
                    dur_i = float(durations[i]) if durations[i] else None  # type: ignore[index]
                except (TypeError, ValueError):
                    dur_i = None
            if dur_i and dur_i > 0:
                parts.append(f"anullsrc=r=48000:cl=stereo:d={dur_i:.3f}[a{i}]")
            else:
                raise ValueError(
                    f"分镜序号 {i} 无音频流且探不到时长，无法补静音"
                    f"（无限静音源会让合成永不结束）；请检查该分镜视频是否损坏"
                )
        else:
            parts.append(f"[{i}:a]aformat=sample_fmts=fltp:channel_layouts=stereo,aresample=48000[a{i}]")
    if n == 1:
        v_final, a_base = "[v0]", "[a0]"
    elif durations and len(durations) == n and all(d for d in durations):
        assert durations is not None
        v_prev, a_prev = "[v0]", "[a0]"
        offset = float(durations[0])  # type: ignore[arg-type]
        for i in range(1, n):
            v_out, a_out = f"[x{i}]", f"[m{i}]"
            parts.append(
                f"{v_prev}[v{i}]xfade=transition=fade:"
                f"duration={XFADE_DURATION}:offset={offset - XFADE_DURATION:.3f}{v_out}"
            )
            parts.append(
                f"{a_prev}[a{i}]acrossfade=d={XFADE_DURATION}{a_out}"
            )
            v_prev, a_prev = v_out, a_out
            offset += float(durations[i]) - XFADE_DURATION  # type: ignore[arg-type]
        v_final, a_base = v_prev, a_prev
    else:
        # 降级：concat
        vcat = "".join(f"[v{i}]" for i in range(n))
        acat = "".join(f"[a{i}]" for i in range(n))
        parts.append(f"{vcat}concat=n={n}:v=1:a=0[vcat]")
        parts.append(f"{acat}concat=n={n}:v=0:a=1[acat]")
        v_final, a_base = "[vcat]", "[acat]"
    # 总时长（用于 dub apad whole_dur）：xfade 终时长=sum-0.3*(n-1)，concat=sum；
    # 未知时长则纯 apad（保证 delay 后不被截断，amix longest 保 bleed 不断尾）。
    total: Optional[float] = None
    if durations and len(durations) == n and all(d for d in durations):
        try:
            s = sum(float(d) for d in durations)  # type: ignore[arg-type]
            if n > 1:
                s -= XFADE_DURATION * (n - 1)
            if s > 0:
                total = s
        except (TypeError, ValueError):
            total = None
    dub_labels: list[str] = []
    for j, (idx, ms) in enumerate(dubs or []):
        ms = max(0, int(ms))
        parts.append(
            f"[{idx}:a]aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"aresample=48000,adelay={ms}|{ms}[dub{j}]"
        )
        # dub 在 amix 前加 apad 保 bleed：delay 后不被截断（分两步以保留 adelay 子串可断言）
        if total is not None:
            parts.append(f"[dub{j}]apad=whole_dur={total:.3f}[dub{j}p]")
        else:
            parts.append(f"[dub{j}]apad[dub{j}p]")
        dub_labels.append(f"[dub{j}p]")
    if dub_labels:
        parts.append(
            f"{a_base}{''.join(dub_labels)}"
            f"amix=inputs={1 + len(dub_labels)}:duration=longest:"
            f"dropout_transition=0[amixed]"
        )
        parts.append("[amixed]loudnorm[aout]")
    else:
        parts.append(f"{a_base}loudnorm[aout]")
    return ";".join(parts), v_final, "[aout]"


def _subtitles_path(srt_path: Optional[str | Path]) -> str:
    """字幕路径转义（存在才回）；调用方负责链到视频输出 label 上。"""
    if not srt_path:
        return ""
    p = str(srt_path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    if not Path(str(srt_path)).exists():
        return ""
    return p


def build_mux_command(
    clips: Sequence[str | Path],
    srt_path: Optional[str | Path],
    out_path: str | Path,
    durations: Optional[Sequence[Optional[float]]] = None,
    dub_tracks: Optional[Sequence[tuple[str | Path, float]]] = None,
    audio_present: Optional[Sequence[bool]] = None,
) -> list[str]:
    """构造 ffmpeg 命令（不执行；供 dry_run/单测与排错）。

    dub_tracks=[(wav 路径, 开始秒)]：存在的 wav 追加为输入，按开始秒 adelay
    对齐后 apad 保 bleed 再与原音频 amix(longest)；缺文件的镜自动跳过
    （仅 is_file 判断，runner 已做 size>0 过滤）；全缺则走无混音旧形状。
    字幕链到视频输出 label 上（勿缀音频链，否则绑 pad 失败）。
    缺音频的视频输入用定长 anullsrc 补静音（时长取 ffprobe 实测；
    audio_present 显式传入时优先使用，默认自动探测；无音频又探不到时长直接抛错）。
    """
    clips = [str(c) for c in clips]
    if not clips:
        raise ValueError("clips 不能为空")
    dubs: list[tuple[int, int]] = []
    extra_inputs: list[str] = []
    for wav, start_s in dub_tracks or []:
        if not Path(str(wav)).is_file():
            continue
        try:
            ms = max(0, int(round(float(start_s) * 1000)))
        except (TypeError, ValueError):
            continue
        dubs.append((len(clips) + len(extra_inputs), ms))
        extra_inputs.append(str(wav))
    if audio_present is None:
        try:
            audio_present = [has_audio_stream(c) for c in clips]
        except Exception:
            audio_present = None  # 降级：build_mux_filter 内按全 True 旧行为
    filt, vout, aout = build_mux_filter(len(clips), durations, dubs or None, audio_present)
    srt = _subtitles_path(srt_path)
    if srt:
        filt += f";{vout}subtitles='{srt}'[vsub]"
        vout = "[vsub]"
    cmd: list[str] = []
    for c in clips:
        cmd += ["-i", c]
    for w in extra_inputs:
        cmd += ["-i", w]
    cmd += [
        "-filter_complex", filt,
        "-map", vout, "-map", aout,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-s", f"{WIDTH}x{HEIGHT}",
        "-c:a", "aac", "-movflags", "+faststart",
        "-y", str(out_path),
    ]
    return ["ffmpeg"] + cmd


def verify_final(
    out_path: str | Path,
    expected_seconds: Optional[float] = None,
    tolerance: float = 1.0,
) -> dict:
    """强校验成品（供 mux_clips/runner 调用）。

    检查：存在、size>0、ffprobe 有 video 流 + audio 流、
    时长误差<=tolerance（expected 为 None 则跳过时长项）。
    失败 raise RuntimeError（含具体哪项不过）；成功返回 dict
    {path/size/duration/has_video/has_audio/expected_seconds/message}。
    ffprobe 缺失时至少做存在+非空检查并在 message 注明“未验时长/流（缺ffprobe）”。
    """
    p = Path(str(out_path))
    if not p.is_file():
        raise RuntimeError(f"成品校验失败：文件缺失 {p}（ffmpeg 未产出？）")
    try:
        size = p.stat().st_size
    except OSError as exc:
        raise RuntimeError(f"成品校验失败：无法读取 {p}：{exc}") from exc
    if size <= 0:
        raise RuntimeError(f"成品校验失败：文件为空 {p}（0 字节残留）")
    if shutil.which("ffprobe") is None:
        return {
            "path": str(p),
            "size": size,
            "duration": None,
            "has_video": None,
            "has_audio": None,
            "expected_seconds": expected_seconds,
            "message": "存在+非空通过，未验时长/流（缺ffprobe）",
        }
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "stream=codec_type",
                "-show_entries", "format=duration",
                "-of", "json", str(p),
            ],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"成品校验失败：ffprobe 执行失败 {p}：{exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"成品校验失败：ffprobe 非 0 (exit={proc.returncode}) {p}："
            f"{(proc.stderr or '')[-500:]}"
        )
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError as exc:
        raise RuntimeError(f"成品校验失败：ffprobe 输出非 JSON {p}：{exc}") from exc
    streams = data.get("streams", []) or []
    has_video = any(s.get("codec_type") == "video" for s in streams)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    if not has_video:
        raise RuntimeError(f"成品校验失败：缺 video 流 {p}（streams={streams}）")
    if not has_audio:
        raise RuntimeError(f"成品校验失败：缺 audio 流 {p}（混音/映射失败？）")
    duration: Optional[float] = None
    try:
        raw = (data.get("format", {}) or {}).get("duration")
        duration = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        duration = None
    if expected_seconds is not None:
        try:
            exp = float(expected_seconds)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"成品校验失败：期望时长非法 {expected_seconds!r}：{exc}") from exc
        if duration is None:
            raise RuntimeError(f"成品校验失败：探不到时长 {p}（无法比对期望 {exp:.2f}s）")
        if abs(duration - exp) > float(tolerance):
            raise RuntimeError(
                f"成品校验失败：时长 {duration:.2f}s 与期望 {exp:.2f}s 误差 "
                f"{abs(duration - exp):.2f}s，超过容差 {float(tolerance):.2f}s"
            )
    return {
        "path": str(p),
        "size": size,
        "duration": duration,
        "has_video": True,
        "has_audio": True,
        "expected_seconds": expected_seconds,
        "message": "校验通过",
    }


def mux_clips(
    clips: Sequence[str | Path],
    srt_path: Optional[str | Path],
    out_path: str | Path,
    dry_run: bool = False,
    timeout: float = 600,
    dub_tracks: Optional[Sequence[tuple[str | Path, float]]] = None,
) -> str:
    """合成 clips + 配音 + 字幕 → out_path(720x1280 final.mp4)。返回 out 路径字符串。

    成功后调 verify_final(out, expected_seconds=None) 做基础校验
    （存在+非空+流）；runner 会再按剧本总时长调一次做时长校验。
    dry_run 仅返回命令字符串，不校验不执行。
    """
    clips = list(clips)
    if not clips:
        raise ValueError("clips 不能为空")
    for c in clips:
        if not Path(str(c)).exists():
            raise FileNotFoundError(f"分镜视频缺失: {c}（不重跑生成，先补产物）")
    if shutil.which("ffmpeg") is None:
        raise FileNotFoundError("未找到 ffmpeg，请先安装并加入 PATH")
    out = Path(str(out_path))
    out.parent.mkdir(parents=True, exist_ok=True)
    durations = [probe_duration(c) for c in clips]
    try:
        audio_present = [has_audio_stream(c) for c in clips]
    except Exception:
        audio_present = None  # 降级：按全 True 旧行为
    cmd = build_mux_command(clips, srt_path, out, durations, dub_tracks, audio_present)
    if dry_run:
        return " ".join(cmd)
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-2000:]
        raise RuntimeError(
            f"ffmpeg 合成失败 (exit={proc.returncode})，不重跑生成，"
            f"请修 srt/路径后重 mux。stderr 尾部:\n{tail}"
        )
    # 基础强校验（存在+非空+流）；时长由 runner 按剧本总时长二次校验
    verify_final(out, expected_seconds=None)
    return str(out)
