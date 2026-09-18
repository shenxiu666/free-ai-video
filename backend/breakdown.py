"""AI 分镜：小说/剧本原文 → script.json 初稿（docs/04）。

流程：用户粘贴原文 → 文本模型按本模块 prompt 输出严格 JSON → coerce
归一化（重算 start、收敛末镜时长保证 sum==total）→ orchestrator.validate_script
校验 → 前端分镜表展示，用户可再改。
`script.json` 仍为流水线唯一真源，本模块只产初稿、不落盘。

生成模式由 AI 逐镜规划（reference/keyframe 为主，text 保底）：
- reference：识别角色立绘/场景图（images 具体路径，≤5）；
- keyframe：相邻镜链式衔接（first_frame/last_frame 至少其一）；
- 初稿阶段永不因缺图降级：AI 判要图即保留要图，缺文件由
  find_missing_assets 标出，前端走“用户补传 / AI 生成”闭环补齐。
"""

from __future__ import annotations

import json
import math
import re

SOURCE_MAX_CHARS = 12000  # 原文截断上限（超长只取前部并标注）

# cast 外貌注入约束：单角色 appearance 截 60 字，多角色分号连，总长封顶 200 字
CAST_APPEARANCE_PER_MAX = 60
CAST_APPEARANCE_TOTAL_MAX = 200

# 场景描述注入约束（镜像角色）：单场景截 60 字，总长封顶 200 字
SCENE_DESC_PER_MAX = 60
SCENE_DESC_TOTAL_MAX = 200

# 分镜图占位（渲染时 _run_image 生成，不算缺图）：images/s01.png；
# 兼容前端旧链式写法 shots/s01_last.png（读态视为占位）。
_FRAME_PLACEHOLDER_RE = re.compile(r"^images/s\d+\.png$", re.IGNORECASE)
_FRAME_LEGACY_RE = re.compile(r"^shots/s\d+_last\.png$", re.IGNORECASE)


def _validate_durations(durations: list[float] | None,
                        total_seconds: float) -> list[float] | None:
    """校验外传可变镜表（plan 透传用）：非法直接 ValueError，不静默透传。

    要求非空、每镜 4-12（末镜可小数但仍须 4-12）、求和精确等于 total。
    """
    if durations is None:
        return None
    durs = list(durations)
    if not durs:
        raise ValueError("durations 须为非空数组")
    total = float(total_seconds)
    for i, d in enumerate(durs):
        if isinstance(d, bool) or not isinstance(d, (int, float)):
            raise ValueError(f"durations[{i}]={d!r} 须为数字")
        f = float(d)
        if not (4 <= f <= 12):
            raise ValueError(f"durations[{i}]={d!r} 须在 4-12 内（plan 非法，拒绝透传）")
    if abs(sum(float(d) for d in durs) - total) > 1e-6:
        raise ValueError(
            f"durations 求和 {sum(float(d) for d in durs)} != total_seconds={total}"
            f"（plan 非法，拒绝透传）")
    return durs


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
                           source_text: str,
                           durations: list[float] | None = None,
                           style_effective: str | None = None,
                           characters_summary: str = "",
                           prev_summary: str = "",
                           cast_plan: list[str] | None = None,
                           scenes_summary: str = "",
                           asset_lines: str = "",
                           scene_plan: list[str] | None = None) -> tuple[str, str]:
    """返回 (system, user)；user 内含明确到每镜的时长表与 JSON 骨架。

    可选参数（系列/规划联动用，无参回退现有均匀逻辑）：
    - durations：可变镜时长表（元素 4-12，末镜可小数），提供则直接采用，
      不再按 clip_seconds 均匀切分；非法（越界/求和不等）直接 ValueError；
    - style_effective：实际生效风格（提供则覆盖 style 进题面与默认 prompt）；
    - characters_summary：角色摘要（原样注入“角色锚点”段）；
    - prev_summary：前集梗概（截断 <=300 字后注入）。
    - cast_plan：本集出场角色名表（注入“本集出场”段，要求每镜按名单写 cast[]）。
    - scenes_summary：场景摘要（原样注入“场景锚点”段）；
    - asset_lines：可用资产清单（角色主图/场景主图具体路径，有图列路径、
      无图列“无图”，AI 只能抄写其中路径，不编造）；
    - scene_plan：本集出场场景名表（要求每镜按名单写 scene[]）。
    """
    if durations is not None:
        durations = _validate_durations(list(durations), total_seconds)
        if not durations:
            raise ValueError("durations 须为非空数组")
    else:
        durations = clip_plan(total_seconds, clip_seconds)
    eff_style = (style_effective or "").strip() or (style or "").strip()
    count = len(durations)
    vertical = aspect != "16:9"
    src = (source_text or "").strip()
    truncated = len(src) > SOURCE_MAX_CHARS
    if truncated:
        src = src[:SOURCE_MAX_CHARS]
    dur_lines = "\n".join(f"- {durations[i]}" for i in range(count))
    system = ("你是竖屏短剧分镜师。只输出一个合法 JSON 对象，不要输出任何解释、"
              "前后缀与 Markdown 围栏。所有字符串用中文。")
    cast_names = [str(x or "").strip() for x in (cast_plan or [])
                  if str(x or "").strip()]
    cast_block = ""
    if cast_names:
        cast_block = ("【本集出场（cast_plan，每镜 cast[] 只许写其中名字，可空）】\n"
                      + "\n".join(f"- {n}" for n in cast_names) + "\n")
    scene_names = [str(x or "").strip() for x in (scene_plan or [])
                   if str(x or "").strip()]
    scene_block = ""
    if scene_names:
        scene_block = ("【本集出场场景（scene_plan，每镜 scene[] 只许写其中名字，可空）】\n"
                       + "\n".join(f"- {n}" for n in scene_names) + "\n")
    asset_block = ""
    if asset_lines and str(asset_lines).strip():
        asset_block = ("【可用资产（reference images / keyframe 帧只能抄写以下路径或 http(s) 直链，不编造）】\n"
                       + str(asset_lines).strip() + "\n")
    scenes_anchor = ""
    if scenes_summary and str(scenes_summary).strip():
        scenes_anchor = f"【场景锚点（沿用，不重造场景）】\n{str(scenes_summary).strip()}\n"
    user = f"""把下面的小说/剧本原文改编成短剧分镜，共 {count} 镜。

【成片规格】
- 剧名：{title}
- 总时长：{total_seconds}s，画幅 {aspect}，风格 {eff_style or '未指定'}
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
      "narration": "本镜旁白（可空，无旁白则填空字符串）",
      "dialogue": "本镜人物原话（可空，无对白则填空字符串，保留原文不改写不截断）",
      "speaker": "说话人（可选，可空）",
      "cast": ["本镜出场角色名（角色名数组，可空，无出场填 []）"],
      "scene": ["本镜出场场景名（场景名数组，可空，无场景填 []）"],
      "image_prompt": "[主体]…+[场景]…+[风格]{eff_style or '…'}+[光照]…+[构图]{'竖构图' if vertical else '横构图'}+[质量]1K,高细节",
      "video_prompt": "[主体]…+[动作]…+[场景]…+[运镜]缓慢推镜+[光照]…+[风格]{eff_style or '…'}",
      "video_mode": "reference",
      "first_frame": "keyframe 模式首帧（images/<上一镜id>.png 或资产清单内路径，可空）",
      "last_frame": "keyframe 模式尾帧（可选，可空）",
      "images": ["reference 模式参照图（资产清单内路径或 http(s) 直链，1-5 个）"],
      "audios": []
    }}
  ]
}}

 【硬规则】
1. clips 恰好 {count} 镜，id 为 s01…s{count:02d}，duration 逐镜照抄上面的时长表，start 从 0 累加。
2. 生成模式由你逐镜规划（三选一，reference/keyframe 为主）：
   a. 需要识别角色立绘/场景图 → "reference"，必须带 images（1-5 个，只能抄【可用资产】内路径或 http(s) 直链，去重；video_prompt 用 <Picture N> 指代对应图），同时填 cast[]/scene[] 说明图里是谁/哪；audios 可空（人物对白不填audios）；禁 videos。
   b. 需要镜头连贯（追车/转身/场景延续）→ "keyframe"，必须带 first_frame/last_frame 至少其一（取值为上一镜分镜图占位 images/<上一镜id>.png 或资产清单内路径/http）；首镜禁 keyframe（无上一帧可链，首镜只能 reference/text）。
   c. 无图纯演绎 → "text"，禁带 first_frame/last_frame/images/audios/videos 任何媒体字段。
   d. 即使资产清单标注"无图"，只要镜头需要识别角色/场景，也必须判 reference 并照常填 images + cast[]/scene[]（照抄【可用资产】中的完整相对路径；新角色/场景允许写库内风格的新相对路径，缺文件由服务端标出补齐，不许自行降级为 text）。
3. narration（旁白，可空）与 dialogue（人物原话，string，可空，保留原文不截断）至少其一非空，不可双空；speaker（可选 string，可空）；三者均出自原文情节，不可编造；image_prompt/video_prompt 必须六段式齐全。
4. character_refs 与 scene_refs 为空数组（服务端回填，你不填）。
5. 每镜按【本集出场】与角色锚点填写 cast[]（角色名数组，可空；只写名单内名字，不编造；无出场填 []）；按【本集出场场景】与场景锚点填写 scene[]（场景名数组，可空；只写名单内名字，不编造；无场景填 []）。
6. speaker 只能是"旁白"或角色库名（即【角色锚点】与【本集出场】中列出的可用名清单，原样照抄一个，不改写、不音译、不翻译）；无明确说话人填"旁白"；不在清单中的名字不得作为 speaker。
7. 人名必须用原文逐字原形，禁音译/改写/翻译/拉丁转写（如原文是中文名不得写拼音）。
{f"【角色锚点（沿用，不重造人设）】\n{characters_summary.strip()}\n" if characters_summary and str(characters_summary).strip() else ""}{scenes_anchor}{cast_block}{scene_block}{asset_block}{f"【前集梗概（<=300字，衔接用）】\n{' '.join(str(prev_summary).split())[:300]}\n" if prev_summary and str(prev_summary).strip() else ""}【原文】{'（过长已截断前 12000 字）' if truncated else ''}
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


def _clean_str_list(v: object, limit: int = 5) -> list[str]:
    """字符串数组清洗：trim、去空、去重保序、截断 limit。"""
    if not isinstance(v, list):
        return []
    out: list[str] = []
    for x in v:
        s = str(x or "").strip()
        if s and s not in out:
            out.append(s)
        if len(out) >= max(1, int(limit)):
            break
    return out


def _coerce_scene(v: object) -> list[str]:
    """scene 透传：string[]；单个字符串视为单元素；非标回落 []。"""
    if isinstance(v, str):
        s = v.strip()
        return [s] if s else []
    return _clean_str_list(v, limit=5)


def is_frame_placeholder(s: str) -> bool:
    """是否为分镜图占位（渲染时生成，不算缺图）：images/sNN.png（含 legacy shots/写法）。"""
    t = str(s or "").strip()
    return bool(_FRAME_PLACEHOLDER_RE.match(t) or _FRAME_LEGACY_RE.match(t))


def _is_remote_or_data(s: str) -> bool:
    t = str(s or "").strip().lower()
    return (t.startswith("http://") or t.startswith("https://")
            or t.startswith("data:image/"))


def find_missing_assets(script: dict,
                        available: set[str] | None = None) -> list[dict]:
    """检出初稿中计划引用但库内不存在的图（供前端“补传/AI生成”闭环）。

    - images/sNN.png 占位与 http(s)/data:image 视为合法，不算缺；
    - available 为库内真实存在相对路径集合（角色主图 + 场景主图）；
    - available 为 None/空时：相对路径引用一律算缺（http/占位除外）；
    - 返回 [{clip_id, field, kind, name, planned_path}]，kind 为 character/scene/frame。
    """
    avail = set(str(x or "").strip() for x in (available or set())
                if str(x or "").strip())
    clips = script.get("clips", []) if isinstance(script, dict) else []
    if not isinstance(clips, list):
        return []
    # 名→kind 反查（cast 命中角色、scene 命中场景；都命中按角色计）
    cast_sets: dict[str, set[str]] = {}
    scene_sets: dict[str, set[str]] = {}
    for c in clips:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id", "") or "")
        cast_sets[cid] = {str(x or "").strip() for x in (c.get("cast", []) or [])
                          if str(x or "").strip()}
        sc = c.get("scene", [])
        if isinstance(sc, str):
            sc = [sc]
        scene_sets[cid] = {str(x or "").strip() for x in (sc or [])
                           if str(x or "").strip()}
    missing: list[dict] = []
    for c in clips:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id", "") or "")
        for field in ("images", "first_frame", "last_frame"):
            vals: list[str] = []
            if field == "images":
                v = c.get("images", [])
                vals = [str(x or "").strip() for x in (v or [])
                        if str(x or "").strip()] if isinstance(v, list) else []
            else:
                s = str(c.get(field, "") or "").strip()
                vals = [s] if s else []
            for p in vals:
                if not p or is_frame_placeholder(p) or _is_remote_or_data(p):
                    continue
                if p in avail:
                    continue
                if p.startswith("scenes/"):
                    kind = "scene"
                elif p.startswith("characters/"):
                    kind = "character"
                else:
                    # 按镜 cast/scene 归属推断，无归属按 frame 计
                    if cast_sets.get(cid):
                        kind = "character"
                    elif scene_sets.get(cid):
                        kind = "scene"
                    else:
                        kind = "frame"
                missing.append({"clip_id": cid, "field": field, "kind": kind,
                                "name": "", "planned_path": p})
    return missing


def coerce_script(raw: dict, title: str, total_seconds: float, aspect: str,
                  clip_seconds: str, style: str,
                  durations: list[float] | None = None,
                  style_effective: str | None = None) -> dict:
    """把模型 JSON 归一化为合法 script.json（保证 sum==total、start 连续）。

    - clips 数量/时长以服务端计划为准：缺镜按计划补空镜，多镜截断；
    - durations 提供则采用可变表（系列/规划联动），无参回退现有均匀逻辑；
    - style_effective 提供则覆盖 style 进默认 prompt；
    - narration/dialogue/speaker 透传并 trim（B 路线：对白只进 video_prompt 原生发声，不截断超长）；
    - cast 透传（string[]，非标输入回落 []；orchestrator.validate_script 保持校验）；
    - scene 透传（string[]，单个字符串视为单元素，非标回落 []）；
    - video_mode 透传 text/keyframe/reference（缺省/非法回落 text）；
    - 媒体字段透传并清洗（images 去重截断 5、audios 截断 3、videos/srt_path 丢弃）；
      text 模式带媒体则清媒体（历史行为保持）；reference/keyframe 即使引用
      文件尚不存在也保留原判（初稿永不因缺图降级，缺图由
      find_missing_assets 标出走补齐闭环）；
    - start 一律重算累加；末镜时长收敛使求和精确等于 total。
    """
    total = float(total_seconds)
    per = str(clip_seconds)
    if durations is not None:
        durations = _validate_durations(list(durations), total)
        if not durations:
            raise ValueError("durations 须为非空数组")
    else:
        durations = clip_plan(total, per)
    eff_style = (style_effective or "").strip() or (style or "").strip()
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
        dialogue = str(src.get("dialogue", "") or "").strip()
        speaker = str(src.get("speaker", "") or "").strip()
        cast_raw = src.get("cast", [])
        if isinstance(cast_raw, list):
            cast = [str(x or "").strip() for x in cast_raw
                    if str(x or "").strip()]
        else:
            cast = []
        scene = _coerce_scene(src.get("scene", []))
        image_prompt = str(src.get("image_prompt", "") or "").strip()
        video_prompt = str(src.get("video_prompt", "") or "").strip()
        if not image_prompt:
            image_prompt = (f"[主体]{title}+[场景]待补充+[风格]{eff_style}+"
                            f"[光照]待补充+[构图]{'竖构图' if vertical else '横构图'}"
                            f"+[质量]1K,高细节")
        if not video_prompt:
            video_prompt = (f"[主体]{title}+[动作]待补充+[场景]待补充+"
                            f"[运镜]缓慢推镜+[光照]待补充+[风格]{eff_style}")
        mode_raw = str(src.get("video_mode", "") or "").strip()
        mode = mode_raw if mode_raw in ("text", "keyframe", "reference") else "text"
        first_frame = str(src.get("first_frame", "") or "").strip()
        last_frame = str(src.get("last_frame", "") or "").strip()
        images = _clean_str_list(src.get("images", []), limit=5)
        audios = _clean_str_list(src.get("audios", []), limit=3)
        clip_obj: dict = {
            "id": cid,
            "start": round(cursor, 3),
            "duration": dur,
            "narration": narration,
            "dialogue": dialogue,
            "speaker": speaker,
            "cast": cast,
            "scene": scene,
            "image_prompt": image_prompt,
            "video_prompt": video_prompt,
            "video_mode": mode,
        }
        if mode == "text":
            # 纯文本禁媒体（历史行为：静默清洗）
            pass
        elif mode == "keyframe":
            if first_frame:
                clip_obj["first_frame"] = first_frame
            if last_frame:
                clip_obj["last_frame"] = last_frame
        elif mode == "reference":
            if images:
                clip_obj["images"] = images
            if audios:
                clip_obj["audios"] = audios
        clips.append(clip_obj)
        cursor += dur
    return {
        "title": title,
        "total_seconds": total,
        "aspect": aspect,
        "resolution": "720x1280" if vertical else "1280x720",
        "clip_seconds": per,
        "character_refs": [],
        "scene_refs": [],
        "clips": clips,
    }


def inject_cast_appearance(image_prompt: str, descs: list[str]) -> str:
    """往 image_prompt 的[主体]段注入出场角色 appearance 短描（不破坏六段式）。

    descs 为 ["名：短描", …]；单条截 60 字、多条分号连、总长封顶 200 字后
    追加进[主体]段尾。无 descs / 无[主体]段原样返回。
    """
    items = [str(d or "").strip()[:CAST_APPEARANCE_PER_MAX]
             for d in (descs or []) if str(d or "").strip()]
    if not items:
        return str(image_prompt or "")
    joined = "；".join(items)
    if len(joined) > CAST_APPEARANCE_TOTAL_MAX:
        joined = joined[:CAST_APPEARANCE_TOTAL_MAX]
    prompt = str(image_prompt or "")
    tag = "[主体]"
    i = prompt.find(tag)
    if i < 0:
        return prompt
    start = i + len(tag)
    j = prompt.find("+[", start)
    seg = f"，出场：{joined}"
    if j < 0:
        return prompt + seg
    return prompt[:j] + seg + prompt[j:]


def inject_scene_description(image_prompt: str, descs: list[str]) -> str:
    """往 image_prompt 的[场景]段注入出场场景 description 短描（不破坏六段式）。

    descs 为 ["名：短描", …]；单条截 60 字、多条分号连、总长封顶 200 字后
    追加进[场景]段尾。无 descs / 无[场景]段原样返回。
    """
    items = [str(d or "").strip()[:SCENE_DESC_PER_MAX]
             for d in (descs or []) if str(d or "").strip()]
    if not items:
        return str(image_prompt or "")
    joined = "；".join(items)
    if len(joined) > SCENE_DESC_TOTAL_MAX:
        joined = joined[:SCENE_DESC_TOTAL_MAX]
    prompt = str(image_prompt or "")
    tag = "[场景]"
    i = prompt.find(tag)
    if i < 0:
        return prompt
    start = i + len(tag)
    j = prompt.find("+[", start)
    seg = f"，取景：{joined}"
    if j < 0:
        return prompt + seg
    return prompt[:j] + seg + prompt[j:]
