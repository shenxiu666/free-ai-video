"""TTS 统一接口（docs/04.6）：synthesize(provider, voice, text, srt) -> (wav, srt)。

- 默认 edge-tts（在线免费，中文好）；失败同 voice 重试 1 次，再抛给上层
  手动换 provider（策略 manual，默认不自动切换）。
- 备选接口预留（未默认启用，需人工确认后改 provider 重跑）：
  - Kokoro-82M：CPU 离线兜底，音色少。备选实现位：_synthesize_kokoro()
  - CosyVoice3：高仿克隆，部署重。备选实现位：_synthesize_cosyvoice3()
  - Qwen3-TTS：云端商用友好但收费。备选实现位：_synthesize_qwen3()
  - IndexTTS2：时长卡点准（单镜对齐），先审 license。备选实现位：_synthesize_indextts2()
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape as _xml_escape

DEFAULT_PROVIDER = "edge-tts"
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

_MANUAL_HINT = (
    "请手动确认后改 provider 重跑（策略 manual，不自动切换）"
)


def _fmt_ts(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _estimate_duration(text: str) -> float:
    # 粗估：中文约 4 字/秒，英文约 2.5 词/秒；下限 2s
    n = len(text.strip())
    return max(2.0, n / 4.0)


# ---- B路线旁白SSML轻增强（纯文本→SSML，不破坏原文） ----
# 对白镜保留视频原生音频（调用方不调 TTS）；仅旁白镜走 synthesize。
_EMOTION_PROSODY: dict[str, tuple[str, str]] = {
    "": ("+0%", "+0Hz"),
    "calm": ("-5%", "-2Hz"),
    "gentle": ("-5%", "+0Hz"),
    "serious": ("-3%", "-5Hz"),
    "happy": ("+8%", "+2Hz"),
    "excited": ("+10%", "+5Hz"),
    "sad": ("-8%", "-5Hz"),
}

_BREAK_500 = '<break time="500ms"/>'
_BREAK_800 = '<break time="800ms"/>'


def _emotion_prosody(emotion: str) -> tuple[str, str]:
    key = (emotion or "").strip().lower()
    return _EMOTION_PROSODY.get(key, _EMOTION_PROSODY[""])


def build_narration_ssml(
    text: str, voice: str = DEFAULT_VOICE, emotion: str = ""
) -> str:
    """旁白 SSML 轻增强：按，。！？加 break（，。500ms / ！？800ms）。

    emotion 预留（默认 ""），映射 rate/pitch 小幅变化，未知值回落默认。
    原文 XML 转义后保留标点再缀 break，去标签可还原纯文本，不破坏纯文本路径。
    """
    plain = str(text or "").strip()
    rate, pitch = _emotion_prosody(emotion or "")
    esc = _xml_escape(plain, {'"': "&quot;", "'": "&apos;"})
    esc = (
        esc.replace("，", f"，{_BREAK_500}")
        .replace("。", f"。{_BREAK_500}")
        .replace("！", f"！{_BREAK_800}")
        .replace("？", f"？{_BREAK_800}")
    )
    v = _xml_escape(str(voice or DEFAULT_VOICE), {'"': "&quot;"})
    return (
        f'<speak version="1.0" xml:lang="zh-CN">'
        f'<voice name="{v}">'
        f'<prosody rate="{rate}" pitch="{pitch}">{esc}</prosody>'
        f"</voice></speak>"
    )


def _probe_wav_duration(wav: Path) -> Optional[float]:
    """探 wav 真实音频时长；全失败返回 None，绝不抛。

    顺序：stdlib wave → mutagen（如有）→ ffprobe（如有）；
    无依赖/非 PCM（如 edge-tts mp3 伪 wav）逐级降级，最终 None 由调用方回落字数估算。
    """
    p = Path(str(wav))
    try:
        with contextlib.closing(wave.open(str(p), "rb")) as f:
            try:
                frames = f.getnframes()
                rate = f.getframerate()
            except Exception:
                frames, rate = 0, 0
            try:
                dur = float(frames) / float(rate) if rate else 0.0
            except (TypeError, ValueError, ZeroDivisionError):
                dur = 0.0
            if dur > 0:
                return dur
    except Exception:
        pass
    try:
        try:
            from mutagen import File as _MutFile  # type: ignore
        except Exception:
            _MutFile = None  # type: ignore
        if _MutFile is not None:
            try:
                audio = _MutFile(str(p))
                info = getattr(audio, "info", None) if audio is not None else None
                length = getattr(info, "length", None) if info is not None else None
                if length:
                    dur = float(length)
                    if dur > 0:
                        return dur
            except Exception:
                pass
    except Exception:
        pass
    try:
        if shutil.which("ffprobe") is None:
            return None
        import json as _json

        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json", str(p),
            ],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            return None
        raw = (_json.loads(proc.stdout or "{}").get("format", {}) or {}).get("duration")
        dur = float(raw) if raw else 0.0
        return dur if dur > 0 else None
    except Exception:
        return None
    return None


def _write_srt(
    srt_path: Path, text: str, duration: Optional[float] = None
) -> Path:
    try:
        dur = float(duration) if duration else 0.0
    except (TypeError, ValueError):
        dur = 0.0
    if not dur or dur <= 0:
        dur = _estimate_duration(text)
    srt_path.parent.mkdir(parents=True, exist_ok=True)
    srt_path.write_text(
        f"1\n{_fmt_ts(0)} --> {_fmt_ts(dur)}\n{text.strip()}\n",
        encoding="utf-8",
    )
    return srt_path


def _synthesize_edge_tts(voice: str, text: str, wav_path: Path) -> None:
    try:
        import edge_tts  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "edge-tts 未安装（pip install edge-tts），" + _MANUAL_HINT
        ) from exc

    async def _run() -> None:
        await edge_tts.Communicate(text, voice).save(str(wav_path))

    wav_path.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(_run())


# ---- 备选实现位（manual 切换后由此接入，保持同签名 (voice, text, wav)->None） ----
def _synthesize_kokoro(voice: str, text: str, wav_path: Path) -> None:  # noqa: ARG001
    raise NotImplementedError("Kokoro-82M 离线兜底未接入，" + _MANUAL_HINT)


def _synthesize_cosyvoice3(voice: str, text: str, wav_path: Path) -> None:  # noqa: ARG001
    raise NotImplementedError("CosyVoice3 克隆未接入，" + _MANUAL_HINT)


def _synthesize_qwen3(voice: str, text: str, wav_path: Path) -> None:  # noqa: ARG001
    raise NotImplementedError("Qwen3-TTS 未接入，" + _MANUAL_HINT)


def _synthesize_indextts2(voice: str, text: str, wav_path: Path) -> None:  # noqa: ARG001
    raise NotImplementedError("IndexTTS2 未接入（先审 license），" + _MANUAL_HINT)


_BACKENDS = {
    "edge-tts": _synthesize_edge_tts,
    "kokoro": _synthesize_kokoro,
    "cosyvoice3": _synthesize_cosyvoice3,
    "qwen3-tts": _synthesize_qwen3,
    "qwen3": _synthesize_qwen3,
    "indextts2": _synthesize_indextts2,
    "index-tts-2": _synthesize_indextts2,
}


def synthesize(
    provider: str = DEFAULT_PROVIDER,
    voice: str = DEFAULT_VOICE,
    text: str = "",
    srt_path: str | Path = "out.srt",
    wav_path: Optional[str | Path] = None,
    emotion: str = "",
) -> tuple[str, str]:
    """合成配音并写 SRT。返回 (wav, srt) 路径字符串。

    srt_path 为字幕输出路径；wav 默认由 srt 同目录同名 .wav 派生。
    edge-tts 失败时同 voice 重试 1 次，再抛 RuntimeError 由上层 manual 换源。
    B路线：仅旁白镜调用此函数（SSML轻增强）；对白镜不调TTS、保留视频原音。
    emotion 预留（默认 ""），映射 rate/pitch 小幅变化。
    SRT 回写真实音频时长（wave/mutagen/ffprobe），无依赖降级字数/4估算，绝不抛。
    """
    if not text or not str(text).strip():
        raise ValueError("TTS 文本不能为空")
    provider = (provider or DEFAULT_PROVIDER).strip().lower()
    if provider not in _BACKENDS:
        raise ValueError(
            f"TTS provider 不支持: {provider!r}，支持 {sorted(_BACKENDS)}。"
            + _MANUAL_HINT
        )
    srt = Path(srt_path)
    wav = Path(wav_path) if wav_path else srt.with_suffix(".wav")
    plain = str(text)
    # B路线旁白 SSML 轻增强：edge-tts 走 SSML payload，SRT 仍写纯文本原文；
    # SSML 构造失败则回落纯文本，绝不阻塞纯文本路径。
    if provider == "edge-tts":
        try:
            payload = build_narration_ssml(plain, voice, emotion or "")
        except Exception:
            payload = plain
    else:
        payload = plain
    backend = _BACKENDS[provider]
    last_exc: Optional[Exception] = None
    succeeded = False
    for _attempt in range(2):  # 首次 + 同 voice 重试 1 次
        try:
            backend(voice, payload, wav)
            last_exc = None
            succeeded = True
            break
        except NotImplementedError:
            raise
        except Exception as exc:  # noqa: BLE001 — 重试后统一转 manual 提示
            last_exc = exc
            succeeded = False
    if not succeeded:
        raise RuntimeError(f"TTS 失败（已同 voice 重试 1 次）: {last_exc}。" + _MANUAL_HINT)
    if not wav.exists():
        raise RuntimeError(f"TTS 失败（无 wav 产物）: {wav}。" + _MANUAL_HINT)
    # SRT 写真实音频时长：探不到/无依赖则降级字数估算，绝不抛。
    real_dur: Optional[float] = None
    try:
        real_dur = _probe_wav_duration(wav)
    except Exception:
        real_dur = None
    _write_srt(srt, plain, duration=real_dur)
    return str(wav), str(srt)
