"""AI 分镜：小说/剧本原文 → script.json 初稿（docs/04）。

流程：用户粘贴原文 → 文本模型按本模块 prompt 输出严格 JSON → coerce
归一化（重算 start、收敛末镜时长保证 sum==total、video_mode 收敛为 text）
→ orchestrator.validate_script 校验 → 前端分镜表展示，用户可再改。
`script.json` 仍为流水线唯一真源，本模块只产初稿、不落盘。
"""

from __future__ import annotations

import json
import math

SOURCE_MAX_CHARS = 12000  # 原文截断上限（超长只取前部并标注）


def clip_plan(total_seconds: float, clip_seconds: str) -> list[float]:
    """按总时长/单镜秒算出每镜时长表（末镜收尾，保证求和 == total）。"""
    per = int(clip_seconds)
    total = float(total_seconds)
    count = max(1, math.ceil(total / per))
    durations = [float(per)] * count
    durations[-1] = round(total - per * (count - 1), 3)
    return durations


def build_breakdown_prompt(title: str, total_seconds: float, aspect: str,
                           clip_seconds: str, style: str,
                           source_text: str) -> tuple[str, str]:
    """返回 (system, user)；user 内含明确到每镜的时长表与 JSON 骨架。"""
    durations = clip_plan(total_seconds, clip_seconds)
    count = len(durations)
    vertical = aspect != "16:9"
    src = (source_text or "").strip()
    truncated = len(src) > SOURCE_MAX_CHARS
    if truncated:
        src = src[:SOURCE_MAX_CHARS]
    dur_lines = "\n".join(f"- {durations[i]}" for i in range(count))
    system = ("你是竖屏短剧分镜师。只输出一个合法 JSON 对象，不要输出任何解释、"
              "前后缀与 Markdown 围栏。所有字符串用中文。")
    user = f"""把下面的小说/剧本原文改编成短剧分镜，共 {count} 镜。

【成片规格】
- 剧名：{title}
- 总时长：{total_seconds}s，画幅 {aspect}，风格 {style or '未指定'}
- 每镜时长（秒，必须逐镜照抄，不可增删改）：
{dur_lines}

【输出 JSON 结构（键名固定英文）】
{{
  "title": "{title}",
  "total_seconds": {total_seconds},
  "aspect": "{aspect}",
  "resolution": "{'720x1280' if vertical else '1280x720'}",
  "clip_seconds": "{clip_seconds}",
  "character_refs": [],
  "clips": [
    {{
      "id": "s01",
      "start": 0,
      "duration": {durations[0]},
      "narration": "本镜旁白/台词（源自原文情节，一句话）",
      "image_prompt": "[主体]…+[场景]…+[风格]{style or '…'}+[光照]…+[构图]{'竖构图' if vertical else '横构图'}+[质量]1K,高细节",
      "video_prompt": "[主体]…+[动作]…+[场景]…+[运镜]缓慢推镜+[光照]…+[风格]{style or '…'}",
      "video_mode": "text"
    }}
  ]
}}

【硬规则】
1. clips 恰好 {count} 镜，id 为 s01…s{count:02d}，duration 逐镜照抄上面的时长表，start 从 0 累加。
2. video_mode 全部填 "text"，不要输出 first_frame/last_frame/images/audios/videos/srt_path。
3. narration 必须出自原文情节，不可空；image_prompt/video_prompt 必须六段式齐全。
4. character_refs 为空数组。

【原文】{'（过长已截断前 12000 字）' if truncated else ''}
{src}"""
    return system, user


def extract_json(text: str) -> dict:
    """从模型输出提取 JSON（兼容 ```json 围栏与前后杂文本）。"""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("模型返回为空")
    if raw.startswith("```"):
        lines = raw.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(f"模型返回不是合法 JSON：{raw[:200]}")
        try:
            obj = json.loads(raw[start:end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"模型返回不是合法 JSON：{str(exc)[:200]}") from exc
    if not isinstance(obj, dict):
        raise ValueError("模型返回的 JSON 顶层不是对象")
    return obj


def coerce_script(raw: dict, title: str, total_seconds: float, aspect: str,
                  clip_seconds: str, style: str) -> dict:
    """把模型 JSON 归一化为合法 script.json（保证 sum==total、start 连续）。

    - clips 数量/时长以服务端计划为准：缺镜按计划补空镜，多镜截断；
    - start 一律重算累加；末镜时长收敛使求和精确等于 total；
    - video_mode 一律收敛为 text 并丢弃媒体字段（v1 只产合法初稿）。
    """
    total = float(total_seconds)
    per = str(clip_seconds)
    durations = clip_plan(total, per)
    vertical = aspect != "16:9"
    in_clips = raw.get("clips") if isinstance(raw, dict) else None
    if not isinstance(in_clips, list) or not in_clips:
        raise ValueError("模型返回缺少 clips 数组")
    clips: list[dict] = []
    cursor = 0.0
    for i, dur in enumerate(durations):
        cid = f"s{i + 1:02d}"
        src = in_clips[i] if i < len(in_clips) else {}
        if not isinstance(src, dict):
            src = {}
        narration = str(src.get("narration", "") or "").strip()
        image_prompt = str(src.get("image_prompt", "") or "").strip()
        video_prompt = str(src.get("video_prompt", "") or "").strip()
        if not image_prompt:
            image_prompt = (f"[主体]{title}+[场景]待补充+[风格]{style}+"
                            f"[光照]待补充+[构图]{'竖构图' if vertical else '横构图'}"
                            f"+[质量]1K,高细节")
        if not video_prompt:
            video_prompt = (f"[主体]{title}+[动作]待补充+[场景]待补充+"
                            f"[运镜]缓慢推镜+[光照]待补充+[风格]{style}")
        clips.append({
            "id": cid,
            "start": round(cursor, 3),
            "duration": dur,
            "narration": narration,
            "image_prompt": image_prompt,
            "video_prompt": video_prompt,
            "video_mode": "text",
        })
        cursor += dur
    return {
        "title": title,
        "total_seconds": total,
        "aspect": aspect,
        "resolution": "720x1280" if vertical else "1280x720",
        "clip_seconds": per,
        "character_refs": [],
        "clips": clips,
    }
