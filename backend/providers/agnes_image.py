"""Agnes 图片 Provider：agnes-image-2.5-flash（docs/04.4、docs/05）。

- Endpoint: POST https://apihub.agnes-ai.com/v1/images/generations
- model 固定 agnes-image-2.5-flash
- size 仅 1K/2K/3K/4K + ratio(1:1/3:4/4:3/16:9/9:16/2:3/3:2/21:9)
- extra_body.response_format=url；禁止顶层 response_format；禁止 tags
- 超时 60-360s
- image_prompt 六段式：[主体+场景+风格+光照+构图+质量]

示例 image_prompt（docs/04.2）：
  "[主体]白衣少年山门前+[场景]雪夜石阶+[风格]电影感写实"
  "+[光照]冷月顶光+[构图]竖构图全身+[质量]1K,高细节"
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import httpx

IMAGE_ENDPOINT = "https://apihub.agnes-ai.com/v1/images/generations"
IMAGE_MODEL = "agnes-image-2.5-flash"

ALLOWED_SIZES = frozenset({"1K", "2K", "3K", "4K"})
ALLOWED_RATIOS = frozenset({"1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9"})

MIN_TIMEOUT = 60
MAX_TIMEOUT = 360


def validate_image_params(size: str, ratio: str, timeout: float = 120) -> None:
    """校验 size/ratio/timeout，不合法直接 raise（发请求前阻断，不计费）。"""
    if size not in ALLOWED_SIZES:
        raise ValueError(
            f"image size 非法: {size!r}，只允许 {sorted(ALLOWED_SIZES)}"
        )
    if ratio not in ALLOWED_RATIOS:
        raise ValueError(
            f"image ratio 非法: {ratio!r}，只允许 {sorted(ALLOWED_RATIOS)}"
        )
    if not (MIN_TIMEOUT <= float(timeout) <= MAX_TIMEOUT):
        raise ValueError(
            f"image timeout 非法: {timeout!r}，须在 {MIN_TIMEOUT}-{MAX_TIMEOUT}s 内"
        )


def build_image_prompt(
    subject: str,
    scene: str,
    style: str,
    lighting: str,
    composition: str,
    quality: str = "1K,高细节",
) -> str:
    """按六段式组装 image_prompt：[主体+场景+风格+光照+构图+质量]。"""
    parts = [subject, scene, style, lighting, composition, quality]
    if any(not p or not str(p).strip() for p in parts):
        raise ValueError("image_prompt 六段式均不能为空")
    return (
        f"[主体]{subject}+[场景]{scene}+[风格]{style}"
        f"+[光照]{lighting}+[构图]{composition}+[质量]{quality}"
    )


def build_image_payload(prompt: str, size: str, ratio: str) -> dict:
    """组装请求体。response_format 只放 extra_body；绝不放顶层；绝不带 tags。"""
    if not prompt or not str(prompt).strip():
        raise ValueError("image prompt 不能为空")
    validate_image_params(size, ratio)
    payload = {
        "model": IMAGE_MODEL,
        "prompt": prompt,
        "size": size,
        "ratio": ratio,
        "extra_body": {"response_format": "url"},
    }
    # 防御性断言：红线（docs/05.6 drama-director 校验清单）
    assert "response_format" not in payload, "response_format 禁止放顶层"
    assert "tags" not in payload, "禁止传 tags"
    return payload


def generate_image(
    prompt: str,
    size: str = "1K",
    ratio: str = "9:16",
    api_key: str = "",
    timeout: float = 120,
    client: Optional[Any] = None,
) -> dict:
    """调用 Agnes 生图。

    返回 {"url": ...} 或 {"b64": ...}，必带 generated_by(provider+model+time)。
    """
    if not api_key:
        raise ValueError("缺少 AGNES_API_KEY，无法调用图片模型")
    validate_image_params(size, ratio, timeout)
    payload = build_image_payload(prompt, size, ratio)
    headers = {"Authorization": f"Bearer {api_key}"}

    own_client = client is None
    http = client or httpx.Client(timeout=float(timeout))
    try:
        resp = http.post(IMAGE_ENDPOINT, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    finally:
        if own_client:
            http.close()

    generated_by = (
        f"agnes/{IMAGE_MODEL}@{datetime.now(timezone.utc).isoformat()}"
    )
    # 兼容 url / b64_json 两种返回
    try:
        first = (data.get("data") or [])[0] or {}
    except (AttributeError, IndexError, TypeError):
        first = {}
    if isinstance(first, dict):
        if first.get("url"):
            return {"url": first["url"], "generated_by": generated_by}
        if first.get("b64_json"):
            return {"b64": first["b64_json"], "generated_by": generated_by}
    if isinstance(data, dict) and data.get("url"):
        return {"url": data["url"], "generated_by": generated_by}
    raise RuntimeError(f"图片返回无法解析: {str(data)[:300]}")
