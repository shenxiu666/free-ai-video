"""渲染执行引擎：把 state.json 的 pending/doing/failed 真正跑完。

链路（docs/04）：分镜图（Agnes image）→ 分镜视频（Agnes video，串行）
→ 配音（TTS）→ 合成（ffmpeg concat+xfade+loudnorm+字幕）。

- 每剧一个后台线程；`_RUNNING` 防重入（start 已在跑返回 False，前端收 409）。
- 进度经 `mark_stage` 写 state.json（SSE 本来就在推）+ `render.log` 写人话日志。
- 同一 Key：提交 → 轮询到终态 → 归还，中途不放手（`VIDEO_LOCK` 全局串行，
  相邻提交 pacing 65s 覆盖 RPM 窗口）；租约延到覆盖轮询，防长任务被回收。
- 提交 429/超时：退避重提同一镜（最多 3 次）；轮询 429：按 Retry-After
  睡后继续问同一单，不丢任务（等待不计入轮询总超时）。
- 桶空但有活 Key：等 pacing 重试；真全灭（全作废/用光）才停并明示。
- 单镜失败不拦整剧（记 failed 继续下一镜），最后汇总；先拿 Key 再标 doing。
"""

from __future__ import annotations

import base64
import json
import logging
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:
    from orchestrator import (  # type: ignore
        get_state as _get_state,
        is_stage_done as _is_stage_done,
        mark_stage as _mark_stage,
        video_slot as _video_slot,
        _drama_dir as _drama_dir_fn,
    )
except ImportError:
    try:
        from backend.orchestrator import (  # type: ignore
            get_state as _get_state,
            is_stage_done as _is_stage_done,
            mark_stage as _mark_stage,
            video_slot as _video_slot,
            _drama_dir as _drama_dir_fn,
        )
    except ImportError:
        _get_state = _is_stage_done = _mark_stage = None  # type: ignore
        _video_slot = None  # type: ignore
        _drama_dir_fn = None  # type: ignore

try:
    from providers.agnes_image import generate_image as _generate_image  # type: ignore
    from providers.agnes_video import (  # type: ignore
        generate_video as _generate_video,
        poll_video as _poll_video,
        submit_video as _submit_video,
    )
except ImportError:
    try:
        from backend.providers.agnes_image import generate_image as _generate_image  # type: ignore
        from backend.providers.agnes_video import (  # type: ignore
            generate_video as _generate_video,
            poll_video as _poll_video,
            submit_video as _submit_video,
        )
    except ImportError:
        _generate_image = _generate_video = None  # type: ignore
        _poll_video = _submit_video = None  # type: ignore

try:
    from tts import synthesize as _synthesize  # type: ignore
except ImportError:
    try:
        from backend.tts import synthesize as _synthesize  # type: ignore
    except ImportError:
        _synthesize = None  # type: ignore

try:
    from ffmpeg_mux import mux_clips as _mux_clips  # type: ignore
except ImportError:
    try:
        from backend.ffmpeg_mux import mux_clips as _mux_clips  # type: ignore
    except ImportError:
        _mux_clips = None  # type: ignore

try:
    import httpx  # type: ignore
except ImportError:
    httpx = None  # type: ignore

STAGES = ("image", "video", "tts", "mux")
STAGE_CN = {"image": "分镜图", "video": "分镜视频", "tts": "配音", "mux": "合成"}
# 单次等待上限（RPM/冷却重试）：超过则该镜记 failed，不无限卡死
MAX_WAIT_S = 600.0
DOWNLOAD_TIMEOUT = 180.0
# 视频提交 pacing：相邻两次视频取 Key 至少间隔（覆盖 60s RPM 窗口）
VIDEO_PACE_S = 65.0
# 视频提交重试：429/超时最多重提次数（退避 60s×次数）
VIDEO_SUBMIT_ATTEMPTS = 3
# 桶空等待轮次（有活 Key 但桶没 token）：等满 pacing 再试
VIDEO_BUCKET_WAITS = 3

_RUNNING: dict[str, threading.Thread] = {}
_RUNNING_LOCK = threading.Lock()


def is_running(name: str) -> bool:
    with _RUNNING_LOCK:
        t = _RUNNING.get(name)
        return t is not None and t.is_alive()


def _running_names() -> list[str]:
    with _RUNNING_LOCK:
        return [n for n, t in _RUNNING.items() if t.is_alive()]


def start_render(name: str, ctx: dict,
                 only_clips: Optional[list[str]] = None) -> bool:
    """起后台线程跑渲染；在跑返回 False。ctx 见 _Worker（pool/cfgs）。"""
    with _RUNNING_LOCK:
        t = _RUNNING.get(name)
        if t is not None and t.is_alive():
            return False
        th = threading.Thread(target=_run_drama,
                              args=(name, ctx, list(only_clips or [])),
                              name=f"render-{name}", daemon=True)
        _RUNNING[name] = th
        th.start()
        return True


def _run_drama(name: str, ctx: dict, only_clips: list[str]) -> None:
    try:
        _Worker(name, ctx, only_clips).run()
    except Exception as exc:  # noqa: BLE001 — worker 绝不能静默死
        try:
            _Worker.log_static(name, f"渲染线程异常退出：{exc}")
        except Exception:
            pass
    finally:
        with _RUNNING_LOCK:
            if _RUNNING.get(name) is threading.current_thread():
                _RUNNING.pop(name, None)


def shift_srt_time(line: str, offset_s: float) -> str:
    """字幕时间行整体偏移 offset_s 秒（合成拼接用）。"""

    def _shift(ts: str) -> str:
        m = re.match(r"(\d+):(\d+):(\d+),(\d+)", ts.strip())
        if not m:
            return ts
        h, mi, s, ms = (int(m.group(1)), int(m.group(2)),
                        int(m.group(3)), int(m.group(4)))
        total_ms = ((h * 3600 + mi * 60 + s) * 1000 + ms
                    + int(round(offset_s * 1000)))
        total_ms = max(0, total_ms)
        hh, rem = divmod(total_ms, 3600000)
        mm, rem = divmod(rem, 60000)
        ss, mss = divmod(rem, 1000)
        return f"{hh:02d}:{mm:02d}:{ss:02d},{mss:03d}"

    if "-->" not in line:
        return line
    left, right = line.split("-->", 1)
    return f"{_shift(left)} --> {_shift(right)}"


def build_err(exc: BaseException) -> dict:
    """异常 → KeyPool classify 入参 {status, body, retry_after}。"""
    if httpx is not None and isinstance(exc, httpx.HTTPStatusError):
        resp = exc.response
        body = ""
        status = 0
        retry_after = None
        try:
            if resp is not None:
                status = int(resp.status_code)
                body = (resp.text or "")[:300]
                retry_after = resp.headers.get("retry-after")
        except Exception:
            pass
        return {"status": status, "body": body, "retry_after": retry_after}
    return {"status": 0, "body": str(exc)[:300], "retry_after": None}


# ----- reference 参照图解析（docs/04.5：角色库 → 渲染） -----

_REF_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

# 参照图 data URL mime 白名单（仅图片可透传下发；其余 data: 判不可用，
# 在拿 Key 前即失败，不烧配额）。
_DATA_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/webp"})

# 单参照图文件上限 5MB：resolve 会把文件全量读入内存再 base64
# （膨胀约 4/3，且 reference 最多 5 图并发在一镜里），无上限时大图/
# 误配视频文件会吃光 worker 内存甚至 OOM。5MB 覆盖正常立绘
# （通常几十~几百 KB），超限记单图不可用（failed），不整镜失败之外的
# 连带影响，调用方在拿 Key 前即失败故不烧配额。
REF_IMAGE_MAX_BYTES = 5 * 1024 * 1024

logger = logging.getLogger(__name__)

_EP_SUFFIX_RE = re.compile(r"^(.*)\s(E\d+)$", re.IGNORECASE)


def _is_remote_ref(s: str) -> bool:
    t = str(s or "").strip().lower()
    return t.startswith("http://") or t.startswith("https://")


def _data_url_mime(s: str) -> str:
    """取 data URL 的 mime（小写，无参）；非 data: 或解析失败返 ""。"""
    t = str(s or "").strip()
    if not t.lower().startswith("data:"):
        return ""
    head = t.split(",", 1)[0]  # "data:<mime>[;params]"（无逗号即畸形，仍尽力取 mime）
    meta = head[5:].split(";")[0].strip().lower()
    return meta


def _is_data_url(s: str) -> bool:
    """是否为可透传的图片 data URL：仅 mime 白名单内（png/jpeg/webp）。

    非图片 data:（如 text/plain、application/json）返回 False，调用方须
    将其判为 unusable（failed），在拿 Key 前失败，不烧配额。
    """
    return _data_url_mime(s) in _DATA_IMAGE_MIMES


def _split_drama_name(name: str) -> tuple[str | None, str | None]:
    """drama 名按 "<系列> E01" 约定拆出 (系列名, 集 id)，不符返 (None, None)。"""
    try:
        m = _EP_SUFFIX_RE.match(str(name or "").strip())
        if not m:
            return None, None
        series_name = str(m.group(1) or "").strip()
        ep_id = str(m.group(2) or "").strip().upper()
        if not series_name or not ep_id:
            return None, None
        return series_name, ep_id
    except Exception:
        return None, None


def _series_dir_for_drama(name: str, drama_dir: Path) -> Path | None:
    """反推系列目录（best-effort：读 characters/x.png 相对路径用，失败返 None）。"""
    try:
        series_name, _ = _split_drama_name(name)
        if not series_name:
            return None
        try:
            import series as _se  # type: ignore
        except ImportError:
            try:
                import backend.series as _se  # type: ignore
            except ImportError:
                return None
        root = drama_dir.parent if isinstance(drama_dir, Path) else None
        sdir = _se.series_dir(series_name,
                              base_dir=str(root) if root is not None else None)
        return sdir if sdir.is_dir() else None
    except Exception:
        return None


def _load_library_for_drama(name: str, drama_dir: Path) -> list[dict]:
    """best-effort 读系列角色库（供 cast→主图回退；失败返 []）。"""
    try:
        series_name, _ = _split_drama_name(name)
        if not series_name:
            return []
        try:
            import characters as _ch  # type: ignore
        except ImportError:
            try:
                import backend.characters as _ch  # type: ignore
            except ImportError:
                return []
        root = drama_dir.parent if isinstance(drama_dir, Path) else None
        lib = _ch.load_characters(series_name,
                                  base_dir=str(root) if root is not None else None)
        return [c for c in (lib or []) if isinstance(c, dict)]
    except Exception:
        return []


def _load_scene_library_for_drama(name: str, drama_dir: Path) -> list[dict]:
    """best-effort 读系列场景库（供 scene→主图回退；失败返 []）。"""
    try:
        series_name, _ = _split_drama_name(name)
        if not series_name:
            return []
        try:
            import scenes as _sc  # type: ignore
        except ImportError:
            try:
                import backend.scenes as _sc  # type: ignore
            except ImportError:
                return []
        root = drama_dir.parent if isinstance(drama_dir, Path) else None
        lib = _sc.load_scenes(series_name,
                              base_dir=str(root) if root is not None else None)
        return [c for c in (lib or []) if isinstance(c, dict)]
    except Exception:
        return []


def _match_library_char(lib: list[dict], cast_name: str) -> dict | None:
    """按名/别名归一匹配库角色（与 merge 的 char_keys 同规则的轻量实现）。"""
    want = re.sub(r"\s+", "", str(cast_name or "").strip().lower())
    if not want:
        return None
    for c in (lib or []):
        if not isinstance(c, dict):
            continue
        keys: set[str] = set()
        nm = re.sub(r"\s+", "", str(c.get("name", "") or "").strip().lower())
        if nm:
            keys.add(nm)
        aliases = c.get("aliases", [])
        if isinstance(aliases, list):
            for a in aliases:
                k = re.sub(r"\s+", "", str(a or "").strip().lower())
                if k:
                    keys.add(k)
        if want in keys:
            return c
    return None


def _match_scene(lib: list[dict], scene_name: str) -> dict | None:
    """按名 strip 精确匹配库场景（大小写敏感，与 merge 规则一致）。"""
    want = str(scene_name or "").strip()
    if not want:
        return None
    for c in (lib or []):
        if not isinstance(c, dict):
            continue
        if str(c.get("name", "") or "").strip() == want:
            return c
    return None


def _clip_scene_names(clip: dict) -> list[str]:
    """clip.scene 解析为名表（string 单值兼容为单元素；非标返 []）。"""
    clip = clip if isinstance(clip, dict) else {}
    sc = clip.get("scene", [])
    if isinstance(sc, str):
        sc = [sc]
    if not isinstance(sc, list):
        return []
    out: list[str] = []
    for x in sc:
        s = str(x or "").strip()
        if s and s not in out:
            out.append(s)
    return out


def _canonical_frame_ref(s: str, drama_dir: Path) -> str:
    """首尾帧引用归一：旧前端链式写法 shots/<id>_last.png 若对应
    images/<id>.png 真实存在则改写为后者（runner 实际产物路径）；否则原样返回。
    """
    t = str(s or "").strip()
    m = re.match(r"^shots/(s\d+)_last\.png$", t, re.IGNORECASE)
    if not m:
        return t
    cand = drama_dir / "images" / f"{m.group(1).lower()}.png"
    try:
        if cand.is_file():
            return f"images/{m.group(1).lower()}.png"
    except (OSError, RuntimeError, ValueError):
        pass
    return t


def reference_images_for_clip(clip: dict, script: dict, drama_dir: Path,
                               drama_name: str = "",
                               characters: list[dict] | None = None,
                               scenes: list[dict] | None = None) -> list[str]:
    """reference 回退链（合并去重，≤5，未做可用性转换）。

    clip.images（显式，AI 已填优先）+ cast 解析角色库主图 +
    scene 解析场景库主图 + 全局 character_refs/scene_refs，
    按此优先级合并去重后截断 5。
    """
    clip = clip if isinstance(clip, dict) else {}
    script = script if isinstance(script, dict) else {}
    merged: list[str] = []

    def _push(s: str) -> None:
        s = str(s or "").strip()
        if s and s not in merged and len(merged) < 5:
            merged.append(s)

    for x in (clip.get("images") or []):
        _push(str(x or ""))
    if len(merged) < 5:
        cast_names: list[str] = []
        for x in (clip.get("cast") or []):
            s = str(x or "").strip()
            if s and s not in cast_names:
                cast_names.append(s)
        lib = list(characters) if characters is not None else _load_library_for_drama(
            drama_name, drama_dir)
        if cast_names and lib:
            for nm in cast_names:
                if len(merged) >= 5:
                    break
                hit = _match_library_char(lib, nm)
                if not isinstance(hit, dict):
                    continue
                imgs = hit.get("images", [])
                if isinstance(imgs, list):
                    for im in imgs:
                        s = str(im or "").strip()
                        if s:
                            _push(s)
                            break
    if len(merged) < 5:
        scene_names = _clip_scene_names(clip)
        slab = list(scenes) if scenes is not None else _load_scene_library_for_drama(
            drama_name, drama_dir)
        if scene_names and slab:
            for nm in scene_names:
                if len(merged) >= 5:
                    break
                hit = _match_scene(slab, nm)
                if not isinstance(hit, dict):
                    continue
                imgs = hit.get("images", [])
                if isinstance(imgs, list):
                    for im in imgs:
                        s = str(im or "").strip()
                        if s:
                            _push(s)
                            break
    if len(merged) < 5:
        try:
            raw_refs = list(script.get("character_refs", []) or []) + \
                list(script.get("scene_refs", []) or [])
        except Exception:
            raw_refs = []
        if isinstance(raw_refs, list):
            for x in raw_refs:
                if len(merged) >= 5:
                    break
                _push(str(x or ""))
    return merged[:5]


def resolve_reference_images(images: list[str], drama_dir: Path,
                             series_dir: Path | None = None
                             ) -> tuple[list[str], list[str]]:
    """库内相对路径 → 可下发形态（本地文件转 data URL base64）。

    远程 http(s)/图片 data URL 原样透传（非图片 data: 判 failed）；
    本地按 [绝对路径/drama 目录/系列目录] 找文件，命中读字节转
    ``data:<mime>;base64,``；找不到/不可读/超 REF_IMAGE_MAX_BYTES
    （5MB，防全量读入内存 OOM）记 failed。返回 (usable, failed)。
    """
    usable: list[str] = []
    failed: list[str] = []
    bases: list[Path] = []
    if isinstance(drama_dir, Path):
        bases.append(drama_dir)
    if isinstance(series_dir, Path):
        bases.append(series_dir)
    for raw in (images or []):
        s = str(raw or "").strip()
        if not s:
            continue
        if _is_remote_ref(s):
            if s not in usable:
                usable.append(s)
            continue
        if s.lower().startswith("data:"):
            if _is_data_url(s):
                if s not in usable:
                    usable.append(s)
            else:
                logger.warning("参照图 data URL 非图片类型已拒绝（mime=%r）：%s",
                               _data_url_mime(s), s[:60])
                if s not in failed:
                    failed.append(s)
            continue
        mime = _REF_IMAGE_MIME.get(Path(s).suffix.lower(), "image/png")
        cands: list[Path] = []
        try:
            p = Path(s)
            if p.is_absolute():
                cands.append(p)
        except Exception:
            pass
        for b in bases:
            try:
                cands.append(b / s)
            except Exception:
                continue
        hit: Path | None = None
        for c in cands:
            try:
                if c.is_file():
                    hit = c
                    break
            except (OSError, RuntimeError, ValueError):
                continue
        if hit is None:
            if s not in failed:
                failed.append(s)
            continue
        try:
            if hit.stat().st_size > REF_IMAGE_MAX_BYTES:
                logger.warning("参照图超限跳过（%s，%.1fMB>5MB，记单图不可用）：%s",
                               hit.name, hit.stat().st_size / 1048576, s)
                if s not in failed:
                    failed.append(s)
                continue
        except OSError:
            if s not in failed:
                failed.append(s)
            continue
        try:
            data = hit.read_bytes()
        except OSError:
            data = b""
        if not data:
            if s not in failed:
                failed.append(s)
            continue
        if len(data) > REF_IMAGE_MAX_BYTES:
            # stat 与读取间文件被改大（TOCTOU）：同样记单图不可用
            logger.warning("参照图超限跳过（读入%.1fMB>5MB，记单图不可用）：%s",
                           len(data) / 1048576, s)
            if s not in failed:
                failed.append(s)
            continue
        usable.append(f"data:{mime};base64,"
                      f"{base64.b64encode(data).decode('ascii')}")
    return usable, failed


class _Worker:
    """单剧渲染 worker。ctx = {"pool", "image", "video", "tts"}（配置 dict）。"""

    def __init__(self, name: str, ctx: dict, only_clips: list[str]):
        self.name = name
        self.ctx = ctx
        self.only = set(only_clips or [])
        self.pool = ctx.get("pool")
        self.ddir = _drama_dir_fn(name)
        self.script = self._read_script()

    @staticmethod
    def log_static(name: str, msg: str) -> None:
        try:
            ddir = _drama_dir_fn(name)
            ddir.mkdir(parents=True, exist_ok=True)
            line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n"
            with open(ddir / "render.log", "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    def log(self, msg: str) -> None:
        self.log_static(self.name, msg)

    def _read_script(self) -> dict:
        try:
            return json.loads((self.ddir / "script.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _clips(self) -> list[dict]:
        clips = self.script.get("clips", []) if isinstance(self.script, dict) else []
        out = [c for c in clips if isinstance(c, dict) and c.get("id")]
        if self.only:
            out = [c for c in out if str(c.get("id")) in self.only]
        return out

    # ----- Key 池 -----

    def _acquire(self, kind: str) -> Any:
        """取 Key；RPM/冷却耗尽等待重试（有 retry_after），池全灭抛。"""
        while True:
            try:
                return self.pool.acquire(kind)
            except Exception as exc:
                if type(exc).__name__ != "PoolExhausted":
                    raise
                wait = getattr(exc, "retry_after", None)
                if not isinstance(wait, (int, float)) or wait is None:
                    raise RuntimeError(
                        f"{kind} Key 池全灭（无可用、无冷却倒计时）："
                        "请检查密钥池（用光/作废）后再开始") from exc
                wait = min(float(wait) + 1.0, MAX_WAIT_S)
                self.log(f"{kind} 限流，等待 {wait:.0f}s 后重试（免费 RPM 排队中）…")
                time.sleep(wait)

    def _release(self, entry: Any, ok: bool, exc: BaseException | None,
                 cost: float, kind: str, latency_ms: float = 0.0) -> str | None:
        """归还并返回分类标签（ok/A/B/C/unknown），异常吞掉返 None。"""
        try:
            return self.pool.release(entry, ok=ok,
                                     err=None if ok else build_err(exc) if exc else None,
                                     cost=cost, kind=kind, latency_ms=latency_ms)
        except Exception:
            return None

    def _pool_has_live(self) -> bool:
        """池里是否还有没作废、没用光的 Key（方法缺失的老池按“无”处理，保持旧行为）。"""
        fn = getattr(self.pool, "has_live_key", None)
        if fn is None:
            return False
        try:
            return bool(fn())
        except Exception:
            return False

    def _pace_video(self) -> None:
        """相邻两次视频取 Key 至少间隔 VIDEO_PACE_S（覆盖 RPM 窗口主动排队）。"""
        last = float(getattr(self, "_last_video_acquire", 0.0) or 0.0)
        wait = VIDEO_PACE_S - (time.monotonic() - last)
        if wait > 0:
            self.log(f"免费 RPM 排队：等待 {wait:.0f}s 后再取视频 Key…")
            time.sleep(wait)

    def _acquire_video(self) -> Any:
        """取视频 Key：冷却等倒计时；桶空（有活 Key）等 pacing 重试；真全灭才抛。"""
        self._pace_video()
        for round_ in range(1, VIDEO_BUCKET_WAITS + 1):
            try:
                entry = self.pool.acquire("video")
                self._last_video_acquire = time.monotonic()
                return entry
            except Exception as exc:
                if type(exc).__name__ != "PoolExhausted":
                    raise
                wait = getattr(exc, "retry_after", None)
                if isinstance(wait, (int, float)) and wait is not None:
                    w = min(float(wait) + 1.0, MAX_WAIT_S)
                    self.log(f"视频 Key 冷却中，等待 {w:.0f}s 后重试…")
                    time.sleep(w)
                    continue
                if self._pool_has_live() and round_ < VIDEO_BUCKET_WAITS:
                    self.log(f"视频 RPM 桶空（Key 都活着），等待 {VIDEO_PACE_S:.0f}s "
                             f"后重试（第{round_}次）…")
                    time.sleep(VIDEO_PACE_S)
                    continue
                raise RuntimeError(
                    "视频 Key 池全灭（无可用 Key）：请检查密钥池（用光/作废）后再开始"
                ) from exc
        raise RuntimeError("视频 Key 池全灭（无可用 Key）：请检查密钥池后再开始")

    # ----- 下载 -----

    def _download(self, url: str, dest: Path) -> None:
        if httpx is None:
            raise RuntimeError("httpx 缺失，无法下载产物")
        import httpx as _hx

        dest.parent.mkdir(parents=True, exist_ok=True)
        with _hx.stream("GET", url, timeout=DOWNLOAD_TIMEOUT) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)

    def _save_image_result(self, result: dict, dest: Path) -> None:
        if result.get("url"):
            self._download(str(result["url"]), dest)
            return
        if result.get("b64"):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(base64.b64decode(result["b64"]))
            return
        raise RuntimeError("图片返回无 url/b64")

    # ----- 主流程 -----

    def run(self) -> None:
        if _get_state is None or _mark_stage is None:
            self.log("orchestrator 缺失，无法渲染")
            return
        if _generate_image is None or _generate_video is None:
            self.log("Providers 缺失，无法渲染")
            return
        if _video_slot is None:
            self.log("orchestrator 视频串行锁缺失，无法渲染")
            return
        clips = self._clips()
        if not clips:
            self.log("无可渲染分镜（剧本为空或过滤后为空）")
            return
        scope = f"（仅 {len(clips)} 镜）" if self.only else f"（共 {len(clips)} 镜）"
        self.log(f"开始渲染{scope}")
        image_cfg = self.ctx.get("image", {}) or {}
        video_cfg = self.ctx.get("video", {}) or {}
        tts_cfg = self.ctx.get("tts", {}) or {}
        for clip in clips:
            cid = str(clip.get("id"))
            try:
                # B路线 mixed 预检前移：旁白+对白同镜直接失败，不烧图片/视频配额
                if self._is_mixed(clip):
                    self._mark(cid, "tts", "failed")
                    self._mark(cid, "video", "failed")
                    raise RuntimeError(f"【{cid}】一镜含旁白+对白，v1 请拆成两镜后再渲染")
                self._run_image(clip, image_cfg)
                # B路线：TTS判定先行（原生写占位srt）
                self._run_tts(clip, tts_cfg)
                self._run_video(clip, clips, video_cfg)
                self._run_clip_mux(clip)
            except Exception as exc:  # noqa: BLE001 — 单镜失败记 failed 继续
                self.log(f"【{cid}】失败：{exc}（已记 failed，可单独重试）")
        self._run_mux(clips)
        self.log("渲染流程结束（成功镜不再重跑，失败镜可单独重试）")

    def _need(self, cid: str, stage: str, artifact: Path | None) -> bool:
        try:
            if _is_stage_done(self.name, cid, stage,
                              str(artifact) if artifact else None):
                return False
        except Exception:
            pass
        try:
            st = _get_state(self.name)
            cur = ((st.get("clips", {}) or {}).get(cid, {}) or {}).get(stage)
            return cur != "done"
        except Exception:
            return True

    def _mark(self, cid: str, stage: str, status: str) -> None:
        try:
            _mark_stage(self.name, cid, stage, status)
        except Exception:
            pass

    def _run_image(self, clip: dict, cfg: dict) -> None:
        cid = str(clip.get("id"))
        dest = self.ddir / "images" / f"{cid}.png"
        if not self._need(cid, "image", dest):
            return
        self._mark(cid, "image", "doing")
        self.log(f"【{cid}】分镜图生成中…")
        entry = self._acquire("image")
        start = time.perf_counter()
        try:
            result = _generate_image(
                str(clip.get("image_prompt", "") or ""),
                size=str(cfg.get("size", "1K") or "1K"),
                ratio=str(cfg.get("ratio", "9:16") or "9:16"),
                api_key=getattr(entry, "raw_key", "") or "")
            self._save_image_result(result, dest)
        except Exception as exc:
            self._release(entry, False, exc, 1, "image",
                          (time.perf_counter() - start) * 1000)
            self._mark(cid, "image", "failed")
            raise RuntimeError(f"分镜图失败：{exc}") from exc
        self._release(entry, True, None, 1, "image",
                      (time.perf_counter() - start) * 1000)
        self._mark(cid, "image", "done")
        self.log(f"【{cid}】分镜图完成")

    def _video_seconds(self, clip: dict) -> str:
        try:
            dur = float(clip.get("duration", 8))
        except (TypeError, ValueError):
            dur = 8.0
        return str(min(12, max(4, round(dur))))

    @staticmethod
    def _clip_dialogue(clip: dict) -> str:
        """B路线对白：缺字段老剧按空处理。"""
        try:
            return str(clip.get("dialogue") or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _clip_narration(clip: dict) -> str:
        try:
            return str(clip.get("narration") or "").strip()
        except Exception:
            return ""

    @classmethod
    def _is_native_dialogue(cls, clip: dict) -> bool:
        """纯对白镜：dialogue非空且narration为空（TTS跳过，视频原生发声）。"""
        return bool(cls._clip_dialogue(clip)) and not cls._clip_narration(clip)

    @classmethod
    def _is_mixed(cls, clip: dict) -> bool:
        """mixed镜：旁白+对白都非空，v1不支持，需拆镜。"""
        return bool(cls._clip_dialogue(clip)) and bool(cls._clip_narration(clip))

    @staticmethod
    def _srt_ts(seconds: float) -> str:
        """秒 → SRT时间戳 HH:MM:SS,mmm。"""
        try:
            total_ms = max(0, int(round(float(seconds) * 1000)))
        except (TypeError, ValueError):
            total_ms = 0
        hh, rem = divmod(total_ms, 3600000)
        mm, rem = divmod(rem, 60000)
        ss, ms = divmod(rem, 1000)
        return f"{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}"

    @classmethod
    def _clip_duration_s(cls, clip: dict, default: float = 8.0) -> float:
        try:
            d = float(clip.get("duration", default) or default)
        except (TypeError, ValueError):
            d = float(default)
        return d if d > 0 else float(default)

    @classmethod
    def _final_video_prompt(cls, clip: dict) -> str:
        """B路线prompt组装：base video_prompt + 对白后缀（老剧缺dialogue按空）。

        后缀与 providers.agnes_video.build_video_prompt dialogue逻辑保持一致：
        ``人物开口说中文“{dialogue}”，口型同步``，超120字截断。
        """
        try:
            base = str(clip.get("video_prompt") or "")
        except Exception:
            base = ""
        dlg = cls._clip_dialogue(clip)
        if not dlg:
            return base
        if len(dlg) > 120:
            dlg = dlg[:120]
        suffix = f"人物开口说中文“{dlg}”，口型同步"
        if not base.strip():
            return suffix
        return f"{base}{suffix}"

    def _run_video(self, clip: dict, all_clips: list[dict], cfg: dict) -> None:
        cid = str(clip.get("id"))
        dest = self.ddir / "videos" / f"{cid}.mp4"
        if not self._need(cid, "video", dest):
            return
        # B路线兜底：mixed镜直接失败，不拿Key不烧视频配额（正常流已由先行TTS判定拦截）
        if self._is_mixed(clip):
            self._mark(cid, "tts", "failed")
            self._mark(cid, "video", "failed")
            raise RuntimeError(
                f"【{cid}】一镜含旁白+对白，请拆镜（v1单镜仅支持旁白或对白其一；未烧视频配额）")
        if _submit_video is None or _poll_video is None:
            self._mark(cid, "video", "failed")
            raise RuntimeError("视频 Provider 缺失")
        mode = str(clip.get("video_mode", "text") or "text")
        seconds = self._video_seconds(clip)
        first_frame = None
        images: list[str] = []
        audios: list[str] = []
        if mode == "keyframe":
            prev_img = self._prev_image(clip, all_clips)
            first_frame = (str(clip.get("first_frame") or "").strip()
                           or (str(prev_img) if prev_img else ""))
            if first_frame:
                # 旧前端链式写法 shots/<id>_last.png → 实际产物 images/<id>.png
                try:
                    first_frame = _canonical_frame_ref(first_frame, self.ddir)
                except Exception:
                    pass
            if not first_frame:
                self._mark(cid, "video", "failed")
                raise RuntimeError("首尾帧模式缺首帧（无 first_frame 且无上一镜成图），请补帧后重试")
        elif mode == "reference":
            # 回退链 clip.images → cast 库主图 → character_refs；先转可用形态，
            # 先校验再拿 Key：转换失败/两空直接 failed，不烧配额。
            cands = reference_images_for_clip(
                clip, self.script, self.ddir, drama_name=self.name)
            usable, failed = resolve_reference_images(
                cands, self.ddir,
                series_dir=_series_dir_for_drama(self.name, self.ddir))
            if failed:
                self._mark(cid, "video", "failed")
                raise RuntimeError(
                    f"角色参照图不可用（{','.join(failed[:3])}"
                    f"{'…' if len(failed) > 3 else ''}）："
                    f"请补立绘后重试（未拿 Key，未烧配额）")
            images = list(usable)
            audios = [str(x) for x in (clip.get("audios") or [])][:3]
            if not images and not audios:
                self._mark(cid, "video", "failed")
                raise RuntimeError(
                    "reference 模式需 images/audios 至少一类非空"
                    "（角色库无可用主图）：请补立绘后重试（未拿 Key，未烧配额）")
        # 先拿到 Key 再标 doing：拿不到直接 failed，状态不说谎
        try:
            entry = self._acquire_video()
        except Exception as exc:
            self._mark(cid, "video", "failed")
            raise
        self._mark(cid, "video", "doing")
        self.log(f"【{cid}】分镜视频提交中（{mode}/{seconds}s）…")
        key = getattr(entry, "raw_key", "") or ""
        # 同一 Key：提交 → 轮询到终态 → 归还，中途不放手；租约延到覆盖轮询
        try:
            entry.lease_until = time.time() + 3600.0
        except Exception:
            pass
        start = time.perf_counter()
        video_id = ""
        try:
            # B路线：clip含dialogue则进video_prompt（老剧缺字段按空），audios保持留空不回喂wav
            video_prompt = self._final_video_prompt(clip)
            video_id, entry, key = self._submit_with_retry(
                cid, clip, seconds, key, mode,
                first_frame, images, audios, entry, start,
                prompt=video_prompt)
            self.log(f"【{cid}】任务已提交（id={video_id}），轮询等成片…")
            result = _poll_video(
                video_id, key, timeout_total=1800, interval=3.0,
                on_throttle=lambda wait, n: self.log(
                    f"【{cid}】轮询被限流，等待 {wait:.0f}s 后继续问同一单"
                    f"（第{n}次，不丢任务）…"))
            url = str(result.get("video_url", "") or "")
            if not url:
                raise RuntimeError("视频完成但无下载地址")
            self._download(url, dest)
        except Exception as exc:
            label = self._release(entry, False, exc, float(seconds), "video",
                                  (time.perf_counter() - start) * 1000)
            self._mark(cid, "video", "failed")
            raise RuntimeError(f"分镜视频失败：{exc}") from exc
        label = self._release(entry, True, None, float(seconds), "video",
                              (time.perf_counter() - start) * 1000)
        self._mark(cid, "video", "done")
        self.log(f"【{cid}】分镜视频完成（Key 归还：{label}）")

    def _submit_with_retry(self, cid: str, clip: dict, seconds: str, key: str,
                           mode: str, first_frame: str | None,
                           images: list[str], audios: list[str],
                           entry: Any, start: float,
                           prompt: str | None = None) -> tuple[str, Any, str]:
        """提交最多 VIDEO_SUBMIT_ATTEMPTS 次：429/超时退避重提，其余直接失败。

        返回 (video_id, 当前持有 entry, 当前 key)：重提换 Key 后调用方继续用
        新 entry 轮询与归还，不泄漏。每次失败都先归还旧 Key 再睡后重取。
        prompt 为空则按clip组装（含dialogue后缀，老剧兼容）；显式传入优先。
        """
        last_exc: Exception | None = None
        for attempt in range(1, VIDEO_SUBMIT_ATTEMPTS + 1):
            try:
                with _video_slot():
                    final_prompt = (prompt if prompt is not None
                                    else self._final_video_prompt(clip))
                    video_id = _submit_video(
                        final_prompt,
                        seconds, key, mode=mode,
                        first_frame=first_frame,
                        images=images or None,
                        audios=audios or None)
                    return video_id, entry, key
            except Exception as exc:
                last_exc = exc
                err = build_err(exc)
                label = self._release(entry, False, exc, float(seconds), "video",
                                      (time.perf_counter() - start) * 1000)
                body120 = str(err.get("body", "") or "")[:120]
                self.log(f"【{cid}】提交第{attempt}次失败（Key 归还：{label}）：{exc} ∥ {body120}")
                retryable = err.get("status") == 429 or "Timeout" in type(exc).__name__
                if not retryable or attempt >= VIDEO_SUBMIT_ATTEMPTS:
                    break
                wait = 60.0 * attempt
                try:
                    ra = float(err.get("retry_after") or 0)
                    if ra > 0:
                        wait = min(ra + 1.0, MAX_WAIT_S)
                except (TypeError, ValueError):
                    pass
                self.log(f"【{cid}】等待 {wait:.0f}s 后重提同一镜…")
                time.sleep(wait)
                try:
                    entry = self._acquire_video()
                    key = getattr(entry, "raw_key", "") or key
                    try:
                        entry.lease_until = time.time() + 3600.0
                    except Exception:
                        pass
                except Exception as exc2:
                    raise RuntimeError(f"重提时无可用 Key：{exc2}") from exc2
        assert last_exc is not None
        raise last_exc

    def _prev_image(self, clip: dict, all_clips: list[dict]) -> Path | None:
        ids = [str(c.get("id")) for c in all_clips]
        try:
            idx = ids.index(str(clip.get("id")))
        except ValueError:
            return None
        if idx <= 0:
            return None
        prev = self.ddir / "images" / f"{ids[idx - 1]}.png"
        return prev if prev.is_file() else None

    def _run_tts(self, clip: dict, cfg: dict) -> None:
        cid = str(clip.get("id"))
        wav = self.ddir / "audio" / f"{cid}.wav"
        srt = self.ddir / "audio" / f"{cid}.srt"
        if not self._need(cid, "tts", wav):
            return
        if _synthesize is None:
            # B路线原生镜也不需TTS模块：先判定，避免缺模块误杀原生镜
            if self._is_native_dialogue(clip):
                pass
            else:
                self._mark(cid, "tts", "failed")
                raise RuntimeError("TTS 模块缺失")
        text = self._clip_narration(clip)
        dialogue = self._clip_dialogue(clip)
        if text and dialogue:
            # mixed镜v1抛错提示拆镜：记failed，不调视频（调用方保证先TTS判定再视频）
            self._mark(cid, "tts", "failed")
            raise RuntimeError(
                f"【{cid}】一镜含旁白+对白，请拆镜（v1单镜仅支持旁白或对白其一；未烧视频配额）")
        if dialogue and not text:
            # B路线纯对白：跳过synthesize，写跨整镜时长占位srt供字幕用
            dur = self._clip_duration_s(clip)
            srt.parent.mkdir(parents=True, exist_ok=True)
            srt.write_text(
                f"1\n{self._srt_ts(0)} --> {self._srt_ts(dur)}\n{dialogue}\n",
                encoding="utf-8")
            self.log(f"【{cid}】原生发声跳过：对白由视频直出，不调TTS（字幕占位已写）")
            self._mark(cid, "tts", "done")
            return
        if not text:
            srt.parent.mkdir(parents=True, exist_ok=True)
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n…\n", encoding="utf-8")
            wav.parent.mkdir(parents=True, exist_ok=True)
            # mark_stage 不支持 message 参数：用日志点名，不硬加不支持的参数
            self.log(f"【{cid}】无台词跳过配音（标 tts done：无台词跳过）")
            self._mark(cid, "tts", "done")
            return
        self._mark(cid, "tts", "doing")
        self.log(f"【{cid}】配音合成中…")
        try:
            _synthesize(str(cfg.get("provider", "edge-tts") or "edge-tts"),
                        str(cfg.get("voice", "zh-CN-XiaoxiaoNeural") or "zh-CN-XiaoxiaoNeural"),
                        text, srt_path=srt, wav_path=wav)
        except Exception as exc:
            self._mark(cid, "tts", "failed")
            raise RuntimeError(f"配音失败：{exc}，可在设置页手动换源后重试") from exc
        self._mark(cid, "tts", "done")
        self.log(f"【{cid}】配音完成")

    def _clip_final_path(self, cid: str) -> Path:
        """单镜成品路径：final_clips/<id>.mp4（ASCII 目录，ffmpeg 省心）。"""
        return self.ddir / "final_clips" / f"{cid}.mp4"

    def _clip_final_ok(self, cid: str) -> bool:
        """单镜成品是否真实存在：文件存在且非空。没有成品，合成就不许算成功。"""
        try:
            p = self._clip_final_path(cid)
            return p.is_file() and p.stat().st_size > 0
        except OSError:
            return False

    def _run_clip_mux(self, clip: dict) -> None:
        """单镜合成：该镜视频+配音+字幕 → final_clips/<id>.mp4。

        硬规则：成品文件不存在/为空就不标 done（只信文件，不信返回值）。
        缺视频记 failed 并抛错；缺配音/缺字幕 warn 点名仍出成品。
        """
        cid = str(clip.get("id"))
        out = self._clip_final_path(cid)
        if _mux_clips is None:
            self._mark(cid, "mux", "failed")
            raise RuntimeError("ffmpeg 模块缺失，无法单镜合成")
        if not self._need(cid, "mux", out):
            return
        if not self._video_ok(cid):
            self._mark(cid, "mux", "failed")
            raise RuntimeError(f"单镜合成失败：缺分镜视频 videos/{cid}.mp4，先补视频")
        video = str(self.ddir / "videos" / f"{cid}.mp4")
        srt = self.ddir / "audio" / f"{cid}.srt"
        try:
            srt_ok = srt.is_file() and srt.stat().st_size > 0
        except OSError:
            srt_ok = False
        wav = self.ddir / "audio" / f"{cid}.wav"
        try:
            wav_ok = wav.is_file() and wav.stat().st_size > 0
        except OSError:
            wav_ok = False
        # B路线：对白原生镜dub置空（不混TTS，即使有残留wav也不盖）
        is_native = self._is_native_dialogue(clip)
        if is_native:
            wav_ok = False
        if not wav_ok and not is_native:
            self.log(f"【{cid}】缺配音，单镜成品将无旁白（仍出片）")
        if not srt_ok:
            self.log(f"【{cid}】缺字幕，单镜成品将无字幕（仍出片）")
        self._mark(cid, "mux", "doing")
        self.log(f"【{cid}】单镜合成中…")
        try:
            _mux_clips([video], str(srt) if srt_ok else None, out,
                       dub_tracks=[(str(wav), 0.0)] if wav_ok else None)
        except Exception as exc:
            self._mark(cid, "mux", "failed")
            raise RuntimeError(f"单镜合成失败：{exc}") from exc
        if not self._clip_final_ok(cid):
            self._mark(cid, "mux", "failed")
            raise RuntimeError(f"单镜合成失败：无成品文件 {out}（不标成功）")
        self._mark(cid, "mux", "done")
        self.log(f"【{cid}】单镜合成完成：final_clips/{cid}.mp4")

    def _full_clips(self) -> list[dict]:
        """剧本全部分镜（不过滤，供单镜补齐判定用）。"""
        clips = self.script.get("clips", []) if isinstance(self.script, dict) else []
        return [c for c in clips if isinstance(c, dict) and c.get("id")]

    def _video_ok(self, cid: str) -> bool:
        """该镜视频是否与分镜对应可用：文件存在且非空（0 字节残留不算）。"""
        try:
            v = self.ddir / "videos" / f"{cid}.mp4"
            return v.is_file() and v.stat().st_size > 0
        except OSError:
            return False

    def _run_mux(self, clips: list[dict]) -> None:
        if _mux_clips is None:
            self.log("ffmpeg 模块缺失，跳过合成")
            return
        if self.only:
            # 单镜重试：全剧视频已齐（刚补上最后一块）则直接出完整版；
            # 还有缺的才跳过——成片只收齐全对应的镜，永不拼残片。
            # 注意：clips 在此已是过滤后的子集，必须按全剧本判定，不能按它计数。
            full = self._full_clips()
            missing = [str(c.get("id")) for c in full if not self._video_ok(str(c.get("id")))]
            if missing:
                self.log(f"单镜重试：还有{len(missing)}镜缺视频"
                         f"（{','.join(missing[:8])}{'…' if len(missing) > 8 else ''}），"
                         f"跳过合成；齐了会自动出成片，或点开始渲染")
                return
            self.log("单镜重试补齐最后一块，全剧已齐，直接合成新版…")
            self._mux_full(full)
            return
        self._mux_full(clips)

    def _mux_full(self, clips: list[dict]) -> None:
        """合成整剧：只有分镜与视频一一对应齐全才动手，缺一镜就不合。

        部分就绪不出残片版本、不碰指针，缺的镜标 failed 并点名。
        """
        assert _mux_clips is not None
        ready: list[str] = []
        missing: list[str] = []
        for clip in clips:
            cid = str(clip.get("id"))
            if self._video_ok(cid):
                ready.append(cid)
            else:
                missing.append(cid)
                self._mark(cid, "mux", "failed")
        if not ready:
            self.log("无完成的分镜视频，跳过合成")
            return
        if missing:
            self.log(f"合成门禁：缺{len(missing)}镜对应视频"
                     f"（{','.join(missing[:4])}{'…' if len(missing) > 4 else ''}），"
                     f"不出残片版本；补齐后自动合成")
            return
        videos = [str(self.ddir / "videos" / f"{cid}.mp4") for cid in ready]
        srt_all = self._build_full_srt(clips, ready)
        # 配音混入：有 wav（存在且非空）的镜按剧本 start 对齐；
        # 缺 wav / 缺 srt 必须点名留痕（warn+顶层 mux_detail），不静默跳过。
        # B路线：对白原生镜dub置空（不混TTS，不计missing_dubs，另记native_dialogue）。
        dub_tracks: list[tuple[str, float]] = []
        missing_dubs: list[str] = []
        missing_srts: list[str] = []
        native_dialogue: list[str] = []
        for clip in clips:
            cid = str(clip.get("id"))
            if cid not in ready:
                continue
            srt = self.ddir / "audio" / f"{cid}.srt"
            try:
                srt_ok = srt.is_file() and srt.stat().st_size > 0
            except OSError:
                srt_ok = False
            if not srt_ok:
                missing_srts.append(cid)
            if self._is_native_dialogue(clip):
                native_dialogue.append(cid)
                continue
            wav = self.ddir / "audio" / f"{cid}.wav"
            try:
                wav_ok = wav.is_file() and wav.stat().st_size > 0
            except OSError:
                wav_ok = False
            if not wav_ok:
                missing_dubs.append(cid)
                continue
            try:
                start_s = float(clip.get("start", 0) or 0)
            except (TypeError, ValueError):
                start_s = 0.0
            dub_tracks.append((str(wav), start_s))
        dub_ok = len(dub_tracks)
        dub_total = len(ready)
        self._record_mux_detail({"missing_dubs": missing_dubs,
                                 "missing_srts": missing_srts,
                                 "dub_ok": dub_ok, "dub_total": dub_total,
                                 "native_dialogue": native_dialogue})
        # 版本化成片：每次合成独立文件存成片/文件夹保留，
        # final.mp4 恒为最新版指针（兼容旧习惯）
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        finals_dir = self.ddir / "成片"
        finals_dir.mkdir(parents=True, exist_ok=True)
        ver = finals_dir / f"final_{stamp}.mp4"
        latest = self.ddir / "final.mp4"
        self.log(f"合成成片中（{len(videos)} 镜，配音 {dub_ok}/{dub_total}轨）…")
        if missing_dubs:
            self.log(f"合成警告：缺配音 {len(missing_dubs)} 镜"
                     f"（{','.join(missing_dubs[:8])}{'…' if len(missing_dubs) > 8 else ''}），"
                     f"仍出片（TTS缺失只警告不阻塞）")
        if missing_srts:
            self.log(f"合成警告：缺字幕 {len(missing_srts)} 镜"
                     f"（{','.join(missing_srts[:8])}{'…' if len(missing_srts) > 8 else ''}），仍出片")
        try:
            _mux_clips(videos, srt_all, ver, dub_tracks=dub_tracks or None)
            try:
                expected_s: float | None = None
                try:
                    expected_s = sum(float(c.get("duration", 0) or 0) for c in clips)
                except (TypeError, ValueError):
                    expected_s = None
                _verify = None
                try:
                    from ffmpeg_mux import verify_final as _vf  # type: ignore
                    _verify = _vf
                except ImportError:
                    try:
                        from backend.ffmpeg_mux import verify_final as _vf2  # type: ignore
                        _verify = _vf2
                    except ImportError:
                        _verify = None
                if _verify is not None:
                    # Builder B 契约：成功返 dict，失败 raise RuntimeError
                    _verify(ver, expected_seconds=expected_s)
                else:
                    if not ver.is_file() or ver.stat().st_size == 0:
                        raise RuntimeError("成片强校验失败：文件不存在或为空（verify_final 缺失，降级检查）")
            except Exception as exc_ver:
                try:
                    if ver.is_file():
                        ver.unlink()
                except OSError:
                    pass
                raise
            shutil.copyfile(ver, latest)
            self._record_final(f"成片/{ver.name}")
        except Exception as exc:
            for cid in ready:
                self._mark(cid, "mux", "failed")
            raise RuntimeError(f"合成失败：{exc}（不重跑生成，修字幕/路径后可单独重跑合成）") from exc
        for cid in ready:
            self._mark(cid, "mux", "done")
        if missing_dubs or missing_srts:
            miss_d = f"缺配音：{','.join(missing_dubs[:8])}" if missing_dubs else ""
            miss_s = f"字幕缺：{','.join(missing_srts[:8])}" if missing_srts else ""
            miss_part = "；".join(p for p in (miss_d, miss_s) if p)
            self.log(f"成片完成：成片/{ver.name}（配音 {dub_ok}/{dub_total}轨，{miss_part}；最新版同步到 final.mp4）")
        else:
            self.log(f"成片完成：成片/{ver.name}（最新版同步到 final.mp4）")

    def _record_final(self, filename: str) -> None:
        """成片留痕：state.json finals 追加 {file, created_at}（只留近 50 条）。

        file 为相对剧目录路径（如 成片/final_20260917-101322.mp4）。
        """
        try:
            sp = self.ddir / "state.json"
            st = json.loads(sp.read_text(encoding="utf-8")) if sp.is_file() else {}
            if not isinstance(st, dict):
                return
            finals = st.get("finals")
            if not isinstance(finals, list):
                finals = []
            finals.append({"file": filename,
                           "created_at": datetime.now().isoformat(timespec="seconds")})
            st["finals"] = finals[-50:]
            sp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, ValueError):
            pass

    def _record_mux_detail(self, detail: dict) -> None:
        """配音/字幕缺轨留痕：state.json 顶层 mux_detail（前端读顶层）。

        detail = {missing_dubs, missing_srts, dub_ok, dub_total, native_dialogue}。
        """
        try:
            sp = self.ddir / "state.json"
            st = json.loads(sp.read_text(encoding="utf-8")) if sp.is_file() else {}
            if not isinstance(st, dict):
                return
            st["mux_detail"] = {
                "missing_dubs": list(detail.get("missing_dubs", []) or []),
                "missing_srts": list(detail.get("missing_srts", []) or []),
                "dub_ok": int(detail.get("dub_ok", 0) or 0),
                "dub_total": int(detail.get("dub_total", 0) or 0),
                "native_dialogue": list(detail.get("native_dialogue", []) or []),
            }
            sp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, ValueError):
            pass

    def _build_full_srt(self, clips: list[dict], ready: list[str]) -> Path | None:
        """各镜 srt 按开始秒偏移拼接（缺台词的镜跳过）。

        B路线：对白原生镜字幕块跨整镜（start→start+duration显示dialogue全文），
        不读占位srt文件，直接按剧本生成。
        """
        out = self.ddir / "full.srt"
        blocks: list[str] = []
        index = 1
        for clip in clips:
            cid = str(clip.get("id"))
            if cid not in ready:
                continue
            # B路线原生镜：整镜时长一块字幕
            if self._is_native_dialogue(clip):
                try:
                    start_s = float(clip.get("start", 0) or 0)
                except (TypeError, ValueError):
                    start_s = 0.0
                dur = self._clip_duration_s(clip)
                end_s = start_s + dur
                dlg = self._clip_dialogue(clip)
                if not dlg:
                    continue
                blocks.append(f"{index}")
                blocks.append(f"{self._srt_ts(start_s)} --> {self._srt_ts(end_s)}")
                blocks.append(dlg)
                index += 1
                continue
            srt = self.ddir / "audio" / f"{cid}.srt"
            if not srt.is_file():
                continue
            try:
                offset = float(clip.get("start", 0) or 0)
            except (TypeError, ValueError):
                offset = 0.0
            try:
                text = srt.read_text(encoding="utf-8-sig")
            except OSError:
                continue
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line or line.isdigit() or "-->" not in line:
                    if line and not line.isdigit() and "-->" not in line:
                        blocks.append(line)
                    continue
                if "-->" in line:
                    blocks.append(f"{index}")
                    blocks.append(shift_srt_time(line, offset))
                    index += 1
        if not blocks:
            return None
        out.write_text("\n".join(blocks) + "\n", encoding="utf-8")
        return out
