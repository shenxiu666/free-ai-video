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
from pathlib import Path
from typing import Optional

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


def _write_srt(srt_path: Path, text: str) -> Path:
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
) -> tuple[str, str]:
    """合成配音并写 SRT。返回 (wav, srt) 路径字符串。

    srt_path 为字幕输出路径；wav 默认由 srt 同目录同名 .wav 派生。
    edge-tts 失败时同 voice 重试 1 次，再抛 RuntimeError 由上层 manual 换源。
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
    backend = _BACKENDS[provider]
    last_exc: Optional[Exception] = None
    succeeded = False
    for _attempt in range(2):  # 首次 + 同 voice 重试 1 次
        try:
            backend(voice, str(text), wav)
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
    _write_srt(srt, str(text))
    return str(wav), str(srt)
