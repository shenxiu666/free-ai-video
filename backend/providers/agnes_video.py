"""Agnes 视频 Provider：agnes-video-2.5-flash（docs/04.4、docs/05）。

- 提交: POST https://apihub.agnes-ai.com/v1/videos
- 轮询: GET https://apihub.agnes-ai.com/agnesapi?video_id=&model_name=agnes-video-2.5-flash
- size 固定字符串 "720P"；seconds 字符串 "4"-"12"
- mode 仅 text/keyframe/reference：
  - text：禁带任何媒体字段
  - keyframe：需 first_frame/last_frame 至少其一
  - reference：需 images(≤5)/audios(≤3) 至少一类非空，且禁 videos
- 轮询 1-2s 一次，直至 completed/failed
- video_prompt 六段式：[主体+动作+场景+运镜+光照+风格]，
  reference 模式引用图用 <Picture N> 指代
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence

import httpx

VIDEO_SUBMIT_ENDPOINT = "https://apihub.agnes-ai.com/v1/videos"
VIDEO_POLL_ENDPOINT = "https://apihub.agnes-ai.com/agnesapi"
VIDEO_MODEL = "agnes-video-2.5-flash"

VIDEO_SIZE = "720P"
ALLOWED_SECONDS = frozenset(str(i) for i in range(4, 13))
ALLOWED_MODES = frozenset({"text", "keyframe", "reference"})

TERMINAL_OK = "completed"
TERMINAL_FAIL = "failed"

# 轮询限流退避：Retry-After 优先，否则指数 5s 起，单次上限 60s
THROTTLE_BASE_S = 5.0
THROTTLE_MAX_S = 60.0


def _throttle_wait(resp: Any, attempt: int) -> float:
    try:
        raw = (resp.headers.get("retry-after") if resp is not None else None)
        if raw is not None and str(raw).strip() != "":
            v = float(str(raw).strip())
            if v >= 0:
                return min(v + 1.0, THROTTLE_MAX_S)
    except (TypeError, ValueError):
        pass
    return min(THROTTLE_BASE_S * (2.0 ** max(0, attempt - 1)), THROTTLE_MAX_S)


def build_video_prompt(
    subject: str,
    action: str,
    scene: str,
    camera: str,
    lighting: str,
    style: str,
    dialogue: str = "",
) -> str:
    """六段式 video_prompt：[主体+动作+场景+运镜+光照+风格]。

    B路线：dialogue 非空时在[动作]段后追加
    ``人物开口说中文“{dialogue}”，口型同步``（超120字截断防prompt爆炸）。
    默认 "" 保持旧6参调用兼容；validate/build_payload 逻辑不动。
    """
    parts = [subject, action, scene, camera, lighting, style]
    if any(not p or not str(p).strip() for p in parts):
        raise ValueError("video_prompt 六段式均不能为空")
    dlg = str(dialogue or "").strip()
    if len(dlg) > 120:
        dlg = dlg[:120]
    action_seg = f"[动作]{action}"
    if dlg:
        action_seg += f"人物开口说中文“{dlg}”，口型同步"
    return (
        f"[主体]{subject}+{action_seg}+[场景]{scene}"
        f"+[运镜]{camera}+[光照]{lighting}+[风格]{style}"
    )


def validate_video_params(
    mode: str,
    seconds: str,
    size: str = VIDEO_SIZE,
    first_frame: Optional[str] = None,
    last_frame: Optional[str] = None,
    images: Optional[Sequence[str]] = None,
    audios: Optional[Sequence[str]] = None,
    videos: Optional[Sequence[str]] = None,
) -> None:
    """mode/seconds/size/媒体字段合规校验，不合法 raise（发请求前阻断）。"""
    if mode not in ALLOWED_MODES:
        raise ValueError(
            f"video mode 非法: {mode!r}，仅允许 {sorted(ALLOWED_MODES)}"
        )
    if not isinstance(seconds, str) or seconds not in ALLOWED_SECONDS:
        raise ValueError(
            f"video seconds 非法: {seconds!r}，须为字符串 "
            f"{sorted(ALLOWED_SECONDS)} 之一"
        )
    if not isinstance(size, str) or size != VIDEO_SIZE:
        raise ValueError(
            f"video size 非法: {size!r}，须为固定字符串 {VIDEO_SIZE!r}"
        )

    has_keyframe = bool(first_frame or last_frame)
    images = list(images or [])
    audios = list(audios or [])
    videos = list(videos or [])

    if mode == "text":
        if has_keyframe or images or audios or videos:
            raise ValueError(
                "text 模式禁止携带任何媒体字段 "
                "(first_frame/last_frame/images/audios/videos)"
            )
    elif mode == "keyframe":
        if not has_keyframe:
            raise ValueError(
                "keyframe 模式需 first_frame/last_frame 至少其一"
            )
    elif mode == "reference":
        if videos:
            raise ValueError("reference 模式禁止携带 videos 字段")
        if len(images) > 5:
            raise ValueError(
                f"reference 模式 images 最多 5 张，当前 {len(images)}"
            )
        if len(audios) > 3:
            raise ValueError(
                f"reference 模式 audios 最多 3 个，当前 {len(audios)}"
            )
        if not images and not audios:
            raise ValueError(
                "reference 模式需 images/audios 至少一类非空"
            )


def build_video_payload(
    prompt: str,
    seconds: str,
    mode: str = "text",
    size: str = VIDEO_SIZE,
    first_frame: Optional[str] = None,
    last_frame: Optional[str] = None,
    images: Optional[Sequence[str]] = None,
    audios: Optional[Sequence[str]] = None,
) -> dict:
    """组装提交请求体（text 模式绝不带媒体字段）。"""
    if not prompt or not str(prompt).strip():
        raise ValueError("video prompt 不能为空")
    validate_video_params(
        mode, seconds, size, first_frame, last_frame, images, audios, None
    )
    payload: dict[str, Any] = {
        "model": VIDEO_MODEL,
        "prompt": prompt,
        "size": size,
        "seconds": seconds,
        "mode": mode,
    }
    if mode == "keyframe":
        if first_frame:
            payload["first_frame"] = first_frame
        if last_frame:
            payload["last_frame"] = last_frame
    elif mode == "reference":
        if images:
            payload["images"] = list(images)
        if audios:
            payload["audios"] = list(audios)
    return payload


def submit_video(
    prompt: str,
    seconds: str,
    api_key: str,
    mode: str = "text",
    size: str = VIDEO_SIZE,
    first_frame: Optional[str] = None,
    last_frame: Optional[str] = None,
    images: Optional[Sequence[str]] = None,
    audios: Optional[Sequence[str]] = None,
    timeout: float = 60,
    client: Optional[Any] = None,
) -> str:
    """提交视频任务，返回 video_id。"""
    if not api_key:
        raise ValueError("缺少 AGNES_API_KEY，无法调用视频模型")
    payload = build_video_payload(
        prompt, seconds, mode, size, first_frame, last_frame, images, audios
    )
    headers = {"Authorization": f"Bearer {api_key}"}
    own_client = client is None
    http = client or httpx.Client(timeout=float(timeout))
    try:
        resp = http.post(VIDEO_SUBMIT_ENDPOINT, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    finally:
        if own_client:
            http.close()
    for key in ("video_id", "id", "task_id"):
        if isinstance(data, dict) and data.get(key):
            return str(data[key])
    if isinstance(data, dict):
        nested = data.get("data") or {}
        if isinstance(nested, dict):
            for key in ("video_id", "id", "task_id"):
                if nested.get(key):
                    return str(nested[key])
    raise RuntimeError(f"视频提交返回无法解析 video_id: {str(data)[:300]}")


def poll_video(
    video_id: str,
    api_key: str,
    timeout_total: float = 1800,
    interval: float = 3.0,
    timeout: float = 30,
    client: Optional[Any] = None,
    on_throttle: Optional[Callable[[float, int], None]] = None,
) -> dict:
    """轮询视频状态直至 completed/failed（默认间隔 3s，允许 1-10s）。

    轮询 429（问得太勤）不杀任务：按 Retry-After（缺省指数退避
    5s→10s→20s…上限 60s）睡后继续问同一个 video_id，并回调
    on_throttle(wait, attempt) 记日志；等待时间不计入总超时。
    failed 直接 raise，不自动重提（由 orchestrator.retry_clip 人工重提）。
    """
    if not video_id:
        raise ValueError("video_id 不能为空")
    if not api_key:
        raise ValueError("缺少 AGNES_API_KEY，无法轮询视频状态")
    if not (1.0 <= float(interval) <= 10.0):
        raise ValueError(f"轮询间隔须为 1-10s，当前 {interval!r}")
    headers = {"Authorization": f"Bearer {api_key}"}
    params = {"video_id": video_id, "model_name": VIDEO_MODEL}
    deadline = time.monotonic() + float(timeout_total)
    throttle_n = 0

    own_client = client is None
    http = client or httpx.Client(timeout=float(timeout))
    try:
        while True:
            try:
                resp = http.get(
                    VIDEO_POLL_ENDPOINT, params=params, headers=headers
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code if exc.response is not None else 0
                if code != 429:
                    raise
                # 轮询限流：退避后继续问同一单，不杀任务、不毒化 Key
                throttle_n += 1
                wait = _throttle_wait(exc.response, throttle_n)
                if on_throttle is not None:
                    on_throttle(wait, throttle_n)
                deadline += wait  # 限流等待不计入总超时
                time.sleep(wait)
                continue
            data = resp.json()
            status = ""
            video_url = ""
            if isinstance(data, dict):
                status = str(
                    data.get("status") or (data.get("data") or {}).get("status") or ""
                ).lower()
                inner = data.get("data") or {}
                if isinstance(inner, dict):
                    video_url = str(
                        inner.get("video_url") or inner.get("url") or ""
                    )
                video_url = video_url or str(data.get("video_url") or data.get("url") or "")
            if status == TERMINAL_OK:
                return {
                    "video_id": video_id,
                    "status": status,
                    "video_url": video_url,
                    "generated_by": (
                        "agnes/" + VIDEO_MODEL + "@"
                        + datetime.now(timezone.utc).isoformat()
                    ),
                }
            if status == TERMINAL_FAIL:
                raise RuntimeError(
                    f"视频任务 failed (video_id={video_id})，"
                    f"请检查 mode 字段合规后经 retry_clip 重提该镜"
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"视频轮询超时 {timeout_total}s (video_id={video_id})"
                )
            time.sleep(float(interval))
    finally:
        if own_client:
            http.close()


def generate_video(
    prompt: str,
    seconds: str,
    api_key: str,
    mode: str = "text",
    size: str = VIDEO_SIZE,
    first_frame: Optional[str] = None,
    last_frame: Optional[str] = None,
    images: Optional[Sequence[str]] = None,
    audios: Optional[Sequence[str]] = None,
    timeout_total: float = 1800,
    interval: float = 3.0,
    client: Optional[Any] = None,
    on_throttle: Optional[Callable[[float, int], None]] = None,
) -> dict:
    """submit + poll 一站式（串行调用；免费 1RPM 下由上层队列串行）。"""
    video_id = submit_video(
        prompt, seconds, api_key, mode, size,
        first_frame, last_frame, images, audios, client=client,
    )
    return poll_video(
        video_id, api_key,
        timeout_total=timeout_total, interval=interval, client=client,
        on_throttle=on_throttle,
    )
