"""短剧 Orchestrator：script.json 唯一真源 + state.json 状态机（docs/04.2/04.8）。

校验（任一失败即阻断）：
- total_seconds >= 60
- clip_seconds 为字符串且 in "4".."12"
- sum(clips.duration) == total_seconds
- character_refs 长度 ≤ 5

状态：outputs/<剧名>/state.json，每镜 image/video/tts/mux:
pending/doing/done/failed。断点续跑：产物存在 + state done 则跳过。
视频走队列串行（免费 1RPM，见 docs/04.3）：VIDEO_LOCK 全局串行 + 等待提示。
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Optional

STAGES = ("image", "video", "tts", "mux")
ALLOWED_CLIP_SECONDS = frozenset(str(i) for i in range(4, 13))

# 视频提交全局串行锁：免费 1RPM 下禁止并行提速，超限退避 60s 由调用方处理。
VIDEO_LOCK = threading.Lock()


def _outputs_root(base_dir: Optional[str | Path] = None) -> Path:
    if base_dir is not None:
        return Path(base_dir)
    # backend/orchestrator.py -> repo_root/outputs
    return Path(__file__).resolve().parent.parent / "outputs"


def _drama_dir(name: str, base_dir: Optional[str | Path] = None) -> Path:
    safe = "".join(c for c in name if c not in '\\/:*?"<>|').strip()
    if not safe:
        raise ValueError("剧名不能为空")
    return _outputs_root(base_dir) / safe


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_script(script: dict) -> bool:
    """校验 script.json 契约，通过返回 True，否则 ValueError。"""
    if not isinstance(script, dict):
        raise ValueError("script 须为 dict")
    total = script.get("total_seconds")
    if not isinstance(total, (int, float)) or isinstance(total, bool):
        raise ValueError("total_seconds 须为数字")
    if float(total) < 60:
        raise ValueError(
            f"total_seconds 下限 60s，当前 {total!r}"
        )
    clip_seconds = script.get("clip_seconds")
    if not isinstance(clip_seconds, str) or clip_seconds not in ALLOWED_CLIP_SECONDS:
        raise ValueError(
            f"clip_seconds 须为字符串 {sorted(ALLOWED_CLIP_SECONDS)} 之一，"
            f"当前 {clip_seconds!r}"
        )
    refs = script.get("character_refs", [])
    if refs is None:
        refs = []
    if not isinstance(refs, list) or len(refs) > 5:
        raise ValueError(
            f"character_refs 须为长度≤5 的列表，当前长度 "
            f"{len(refs) if isinstance(refs, list) else repr(refs)}"
        )
    clips = script.get("clips")
    if not isinstance(clips, list) or not clips:
        raise ValueError("clips 须为非空列表")
    durations = []
    for i, clip in enumerate(clips):
        if not isinstance(clip, dict):
            raise ValueError(f"clips[{i}] 须为 dict")
        if not clip.get("id") or not isinstance(clip.get("id"), str):
            raise ValueError(f"clips[{i}] 缺少 id（字符串）")
        d = clip.get("duration")
        if not isinstance(d, (int, float)) or isinstance(d, bool) or float(d) <= 0:
            raise ValueError(f"clip {clip.get('id')!r} duration 须为正数")
        durations.append(float(d))
        mode = clip.get("video_mode")
        if mode is not None and mode not in ("text", "keyframe", "reference"):
            raise ValueError(
                f"clip {clip.get('id')!r} video_mode 非法: {mode!r}"
            )
    if abs(sum(durations) - float(total)) > 1e-6:
        raise ValueError(
            f"sum(clips.duration)={sum(durations)} != total_seconds={total}"
        )
    return True


def _fresh_state(name: str, script: dict) -> dict:
    return {
        "drama": name,
        "total_seconds": script["total_seconds"],
        "clip_seconds": script.get("clip_seconds"),
        "updated_at": _now_iso(),
        "clips": {
            str(c["id"]): {stage: "pending" for stage in STAGES}
            for c in script["clips"]
        },
    }


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def create_drama(
    name: str, script: dict, base_dir: Optional[str | Path] = None
) -> dict:
    """新建剧：校验 script.json → 落盘 → 初始化/合并 state.json。返回 state。"""
    validate_script(script)
    ddir = _drama_dir(name, base_dir)
    ddir.mkdir(parents=True, exist_ok=True)
    _write_json(ddir / "script.json", script)
    state_path = ddir / "state.json"
    fresh = _fresh_state(name, script)
    if state_path.exists():
        # 断点续跑：保留已有 done/failed，只合并新增分镜
        try:
            old = _read_json(state_path)
            old_clips = old.get("clips", {}) if isinstance(old, dict) else {}
            for cid, stages in fresh["clips"].items():
                if cid in old_clips and isinstance(old_clips[cid], dict):
                    merged = dict(stages)
                    for s in STAGES:
                        if old_clips[cid].get(s) in (
                            "pending", "doing", "done", "failed",
                        ):
                            merged[s] = old_clips[cid][s]
                    fresh["clips"][cid] = merged
        except (json.JSONDecodeError, OSError):
            pass
    fresh["updated_at"] = _now_iso()
    _write_json(state_path, fresh)
    return fresh


def get_state(
    name: str, base_dir: Optional[str | Path] = None
) -> dict:
    """读取 state.json；不存在抛 FileNotFoundError（上层转 404）。"""
    state_path = _drama_dir(name, base_dir) / "state.json"
    if not state_path.exists():
        raise FileNotFoundError(f"剧 {name!r} 不存在（无 state.json）")
    return _read_json(state_path)


def mark_stage(
    name: str,
    clip_id: str,
    stage: str,
    status: str,
    base_dir: Optional[str | Path] = None,
) -> dict:
    """推进单个 stage 状态，返回最新 state。"""
    if stage not in STAGES:
        raise ValueError(f"stage 非法: {stage!r}，允许 {STAGES}")
    if status not in ("pending", "doing", "done", "failed"):
        raise ValueError(f"status 非法: {status!r}")
    state = get_state(name, base_dir)
    clips = state.get("clips", {})
    if clip_id not in clips:
        raise KeyError(f"clip {clip_id!r} 不存在")
    clips[clip_id][stage] = status
    state["updated_at"] = _now_iso()
    _write_json(_drama_dir(name, base_dir) / "state.json", state)
    return state


def is_stage_done(
    name: str,
    clip_id: str,
    stage: str,
    artifact: Optional[str | Path] = None,
    base_dir: Optional[str | Path] = None,
) -> bool:
    """断点续跑判定：state done 且（无产物要求或产物文件存在）才算 done。"""
    try:
        state = get_state(name, base_dir)
    except FileNotFoundError:
        return False
    if state.get("clips", {}).get(clip_id, {}).get(stage) != "done":
        return False
    if artifact is None:
        return True
    return Path(artifact).exists()


def retry_clip(
    name: str,
    clip_id: str,
    base_dir: Optional[str | Path] = None,
    stages: Optional[list[str]] = None,
) -> dict:
    """单镜重试：仅把该镜 failed/doing（默认）重置为 pending，不动成功镜。"""
    state = get_state(name, base_dir)
    clips = state.get("clips", {})
    if clip_id not in clips:
        raise KeyError(f"clip {clip_id!r} 不存在")
    targets = list(stages) if stages else [
        s for s in STAGES if clips[clip_id].get(s) in ("failed", "doing")
    ]
    for s in targets:
        if s not in STAGES:
            raise ValueError(f"stage 非法: {s!r}")
        clips[clip_id][s] = "pending"
    state["updated_at"] = _now_iso()
    _write_json(_drama_dir(name, base_dir) / "state.json", state)
    return state


@contextmanager
def video_slot() -> Generator[None, Any, None]:
    """视频串行槽：免费 1RPM 下串行提交。

    调用方在持有锁期间提交+轮询；等待锁即是在排队（上层应提示用户等待）。
    """
    VIDEO_LOCK.acquire()
    try:
        yield
    finally:
        VIDEO_LOCK.release()
