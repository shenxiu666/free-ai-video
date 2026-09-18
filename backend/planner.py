"""Planner：系列分集规划（Builder M1；docs/04）。

输出契约（纯 Schema、无示例数值，勿照抄数字；数值约束只见硬规则文字）::

    {
      "total_seconds": "<number：auto 模式按原文估算 ≥4；显式模式照抄题面 total>",
      "clip_durations": "<number[]：每项 4-12，非末镜为整数，和==total，镜数≥3 时须含至少两种不同秒数>",
      "style_effective": "<string，非空>",
      "cast_plan": [{"name": "<string>", "role": "<string>", "episodes": ["<string>"]}],
      "market": {
        "hook_3s": "<string，必填非空>",
        "beats": "<string[]：长度==ceil(total/15)，随 total 动态>",
        "beats_per_15s": "<number：由 beats 数推导>",
        "cliffhanger": "<string，必填非空>",
        "risk": "<string，可空>"
      },
      "density": {"chars_per_sec": "<number：8-14>", "dialogue_max": "<number：1-30>"}
    }

- ``clip_durations``：除末镜外须为 4-12 整数，末镜可为小数收尾
  （仍须 4-12），求和精确等于 ``total_seconds``（容差 1e-6）。
- ``market``：hook_3s 必填、每 15s 至少 1 beat
  （``len(beats) >= ceil(total/15)``）、结尾必留悬念（cliffhanger 必填）。
- ``density``：chars_per_sec 8-14，dialogue_max <= 30。
- 超长对白 >45 字必须拆镜：prompt 硬要求 + ``needs_split``/``split_dialogue``
  工具函数供上层/测试判定。
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Optional

MIN_CLIP = 4
MAX_CLIP = 12
DEFAULT_PER = 8


def clip_durations_for_total(total_seconds: float,
                              per: int = DEFAULT_PER) -> list[float]:
    """按总时长算默认镜时长表（整数 4-12，末镜小数收尾，sum==total）。

    循环重切：从 ``ceil(total/per)`` 出发找最近可行镜数（每镜 4-12），
    尾镜 <4 时向前借位重分（不直接合并，避免 12+3=15 越界）。
    断言兜底：返回前校验每镜 4-12 且求和精确等于 total。
    """
    total = float(total_seconds)
    if total <= 0:
        raise ValueError("total_seconds 须为正数")
    if per not in range(MIN_CLIP, MAX_CLIP + 1):
        raise ValueError(f"per 须为 {MIN_CLIP}-{MAX_CLIP} 整数")
    if total < MIN_CLIP:
        raise ValueError(f"total_seconds 过小（<{MIN_CLIP}s），无法按 4-12 切镜")
    # 可行镜数区间：n*4 <= total <= n*12（n=1 时 total 须在 4-12 内）
    lo = max(1, math.ceil(total / MAX_CLIP))
    hi = max(1, math.floor(total / MIN_CLIP))
    if lo > hi:
        raise ValueError(f"total_seconds={total} 无法按 4-12 切镜")
    n0 = max(1, math.ceil(total / per))
    n0 = min(max(n0, lo), hi)

    def _feasible(n: int) -> bool:
        if n == 1:
            return MIN_CLIP <= total <= MAX_CLIP
        k = n - 1
        low = max(MIN_CLIP * k, total - MAX_CLIP)
        high = min(MAX_CLIP * k, total - MIN_CLIP)
        # sum_first 须为整数且落在 [low, high] 内
        return math.ceil(low - 1e-9) <= math.floor(high + 1e-9)

    count: int | None = None
    for step in range(0, max(hi - lo + 1, 1)):
        cands = [n0] if step == 0 else [n0 - step, n0 + step]
        for cand in cands:
            if lo <= int(cand) <= hi and _feasible(int(cand)):
                count = int(cand)
                break
        if count is not None:
            break
    if count is None:
        # 兜底：从 lo..hi 顺序找首个可行（理论上必有）
        for cand in range(lo, hi + 1):
            if _feasible(cand):
                count = cand
                break
    if count is None:
        raise ValueError(f"total_seconds={total} 无法按 4-12 切镜")
    n = int(count)
    if n == 1:
        out1: list[float] = [round(total, 3)]
        if out1[0].is_integer() if isinstance(out1[0], float) else False:
            out1 = [int(out1[0])]
        assert MIN_CLIP <= float(out1[0]) <= MAX_CLIP and abs(float(out1[0]) - total) < 1e-6
        return out1
    k = n - 1
    low = max(MIN_CLIP * k, total - MAX_CLIP)
    high = min(MAX_CLIP * k, total - MIN_CLIP)
    # sum_first 取最接近 per*k 的可行整数（尾<4 时即向前借位）
    target = per * k
    sum_first = int(round(target))
    sum_first = min(max(sum_first, math.ceil(low - 1e-9)), math.floor(high + 1e-9))
    last = round(total - sum_first, 3)
    # 分摊 sum_first 到 k 个 4-12 整数：均分（base/rem），避免前重后轻
    base = sum_first // k
    rem = int(sum_first - base * k)
    assert MIN_CLIP <= base <= MAX_CLIP, (total, per, n, sum_first)
    assert 0 <= rem < k, (total, per, n, sum_first)
    bases: list[int] = [int(base) + (1 if i < rem else 0) for i in range(k)]
    for b in bases:
        assert MIN_CLIP <= b <= MAX_CLIP, (total, per, n, sum_first, bases)
    assert sum(bases) == sum_first, (total, per, n, sum_first, bases)
    durations: list[float] = [float(b) for b in bases] + [float(last)]
    # 归一：整数镜转 int，末镜整数且合法也转 int
    out: list[float] = []
    for i, d in enumerate(durations):
        if i < len(durations) - 1 and float(d).is_integer():
            out.append(int(d))
        elif i == len(durations) - 1 and float(d).is_integer() and MIN_CLIP <= d <= MAX_CLIP:
            out.append(int(d))
        else:
            out.append(round(float(d), 3))
    # 断言兜底：每镜 4-12 且 sum==total
    assert abs(sum(float(d) for d in out) - total) < 1e-6, (out, total)
    for i, d in enumerate(out):
        f = float(d)
        assert MIN_CLIP <= f <= MAX_CLIP, (total, per, out)
        if i < len(out) - 1:
            assert float(d).is_integer(), (total, per, out)
    return out


def needs_split(dialogue: str, limit: int = 45) -> bool:
    """超长对白是否必须拆镜（>limit 字）。"""
    return len((dialogue or "").strip()) > limit


def split_dialogue(dialogue: str, limit: int = 45) -> list[str]:
    """按句读把超长对白拆成 <=limit 的多镜片段（末片可短，不丢字）。

    优先按 。！？；…+换行 断句，单句仍超长则硬切。
    """
    text = (dialogue or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    sentences = [s for s in re.split(r"(?<=[。！？；…\n])", text) if s.strip()]
    if not sentences:
        sentences = [text]
    parts: list[str] = []
    buf = ""
    for s in sentences:
        s = s.strip()
        while len(s) > limit:
            chunk, s = s[:limit], s[limit:]
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(chunk)
        if not buf:
            buf = s
        elif len(buf) + len(s) <= limit:
            buf += s
        else:
            parts.append(buf)
            buf = s
    if buf:
        parts.append(buf)
    return [p for p in parts if p]


def build_planner_prompt(series_name: str, total_seconds: float | None,
                         style: str = "", source_text: str = "",
                         characters_summary: str = "",
                         prev_summary: str = "",
                         episode_title: str = "") -> tuple[str, str]:
    """返回 (system, user)；题面写死 Planner 硬规则。

    total_seconds=None 时为 AI 定时长模式：题面要求 AI 按原文信息量以
    密度 8-14字/s 提议总时长（提议值须 ≥4s，单镜 4-12 整数、末镜可小数
    收尾，beats 数 = ceil(total/15) 随 total 动态）。
    """
    auto_total = total_seconds is None or (
        isinstance(total_seconds, str) and not str(total_seconds).strip())
    total: float = 0.0
    beats_min = 0
    if not auto_total:
        total = float(total_seconds)  # type: ignore[arg-type]
        beats_min = max(1, math.ceil(total / 15))
    src = (source_text or "").strip()
    truncated = len(src) > 12000
    if truncated:
        src = src[:12000]
    system = ("你是竖屏短剧策划（Planner）。只输出一个合法 JSON 对象，不要输出"
              "任何解释、前后缀与 Markdown 围栏。所有字符串用中文。")
    if auto_total:
        user = f"""为系列《{series_name}》{('分集' + episode_title) if episode_title else '新分集'}做规划。

【成片规格】
- 本集总时长：由 AI 按原文信息量自由提议（按 density 8-14字/s 估算，提议值须 ≥4s 单镜下限，clip_durations 求和必须精确等于提议值；题面不提供示例数值，无可照抄）
- 期望风格：{style or '未指定'}（输出 style_effective 给出实际生效风格，不可为空）
- 每 15s 至少 1 个 beat：beats 数 = ceil(total/15)，随提议 total 动态计算

【输出 JSON 结构（键名固定英文，纯 Schema、无示例数值：尖括号内为类型+约束说明，输出时填真实值、勿输出尖括号）】
{{
  "total_seconds": "<number：按原文信息量估算的秒数，须≥单镜下限>",
  "clip_durations": "<number[]：按提议 total 自由切分的每镜秒数表，和须精确等于提议值>",
  "style_effective": "<string：实际生效风格，不可为空>",
  "cast_plan": [{{"name": "<string：角色名>", "role": "<string：主角/配角>", "episodes": ["<string：集号>"]}}],
  "market": {{
    "hook_3s": "<string：开场钩子，必填，不可空>",
    "beats": "<string[]：每15s至少1个，个数==ceil(total/15)，随 total 动态>",
    "beats_per_15s": "<number：由 beats 数推导>",
    "cliffhanger": "<string：结尾悬念，必填，不可空>",
    "risk": "<string：风险提示，可空>"
  }},
  "density": {{"chars_per_sec": "<number：8-14>", "dialogue_max": "<number：1-30>"}}
}}

【硬规则】
1. 先数原文有效字数，再自选 density 8-14字/s 估算提议总时长 total_seconds（须 ≥4s 单镜下限，须为数字）；clip_durations：除末镜外每镜为 4-12 的整数、按剧情节奏自由组合（镜数≥3 时须含至少两种不同秒数，全等凑整校验不通过），末镜可为小数收尾（仍须 4-12），求和精确等于提议总时长；输出前自检求和与镜数多样性。
2. market.hook_3s 必填不可空；beats 个数 = ceil(total/15)（每15s至少1个，随 total 动态）；market.cliffhanger 必填，结尾必须留悬念，不许大团圆收尾。
3. density.chars_per_sec 须在 8-14 之间；density.dialogue_max <= 30。
4. 超长对白 >45 字必须拆成多镜（单镜 dialogue 不许超 45 字），在本集 beats/分镜意图中体现拆分。
5. cast_plan 只列本集出场角色（可空数组）：name 必须是本集原文逐字出现的写法（禁音译/改写/翻译/拉丁转写，不在原文的名字校验不通过）；题面给了【角色锚点】时只许用锚点清单中的名字（含别名写法，不许造新名、不许改写）；role 只填主角/配角。
6. 所有台词/情节/人名必须出自原文，不可编造；风格须落到 style_effective。
7. 只输出 JSON 对象本身，不输出推导过程与解释。
"""
        if characters_summary and str(characters_summary).strip():
            user += f"\n【角色锚点（只许用其中名字，不重造人设、不改写）】\n{str(characters_summary).strip()}\n"
        if prev_summary and str(prev_summary).strip():
            prev = " ".join(str(prev_summary).split())[:300]
            user += f"\n【前集梗概（<=300字，衔接用）】\n{prev}\n"
        user += f"\n【原文】{'（过长已截断前 12000 字）' if truncated else ''}\n{src}"
        return system, user
    user = f"""为系列《{series_name}》{('分集' + episode_title) if episode_title else '新分集'}做规划。

【成片规格】
- 本集总时长：{total}s（clip_durations 求和必须精确等于该值）
- 期望风格：{style or '未指定'}（输出 style_effective 给出实际生效风格，不可为空）
- 每 15s 至少 1 个 beat：本集至少 {beats_min} 个 beats

【输出 JSON 结构（键名固定英文，纯 Schema、无示例数值：尖括号内为类型+约束说明，输出时填真实值、勿输出尖括号）】
{{
  "total_seconds": {total},
  "clip_durations": "<number[]：按 total 自由切分的每镜秒数表，和须精确等于 total>",
  "style_effective": "<string：实际生效风格，不可为空>",
  "cast_plan": [{{"name": "<string：角色名>", "role": "<string：主角/配角>", "episodes": ["<string：集号>"]}}],
  "market": {{
    "hook_3s": "<string：开场钩子，必填，不可空>",
    "beats": "<string[]：每15s至少1个>",
    "beats_per_15s": "<number：由 beats 数推导>",
    "cliffhanger": "<string：结尾悬念，必填，不可空>",
    "risk": "<string：风险提示，可空>"
  }},
  "density": {{"chars_per_sec": "<number：8-14>", "dialogue_max": "<number：1-30>"}}
}}

【硬规则】
1. clip_durations：除末镜外每镜为 4-12 的整数、按剧情节奏自由组合（镜数≥3 时须含至少两种不同秒数，全等凑整校验不通过），末镜可为小数收尾（仍须 4-12），求和精确等于总时长；输出前自检求和与镜数多样性。
2. market.hook_3s 必填不可空；beats 至少 {beats_min} 个（每15s至少1个）；market.cliffhanger 必填，结尾必须留悬念，不许大团圆收尾。
3. density.chars_per_sec 须在 8-14 之间；density.dialogue_max <= 30。
4. 超长对白 >45 字必须拆成多镜（单镜 dialogue 不许超 45 字），在本集 beats/分镜意图中体现拆分。
5. cast_plan 只列本集出场角色（可空数组）：name 必须是本集原文逐字出现的写法（禁音译/改写/翻译/拉丁转写，不在原文的名字校验不通过）；题面给了【角色锚点】时只许用锚点清单中的名字（含别名写法，不许造新名、不许改写）；role 只填主角/配角。
6. 所有台词/情节/人名必须出自原文，不可编造；风格须落到 style_effective。
"""
    if characters_summary and str(characters_summary).strip():
        user += f"\n【角色锚点（只许用其中名字，不重造人设、不改写）】\n{str(characters_summary).strip()}\n"
    if prev_summary and str(prev_summary).strip():
        prev = " ".join(str(prev_summary).split())[:300]
        user += f"\n【前集梗概（<=300字，衔接用）】\n{prev}\n"
    user += f"\n【原文】{'（过长已截断前 12000 字）' if truncated else ''}\n{src}"
    return system, user


def parse_plan_json(text: str) -> dict:
    """从模型输出提取 Planner JSON（复用 breakdown 提取逻辑，缺模块时自实现）。"""
    try:
        import breakdown as _bd  # type: ignore
        return _bd.extract_json(text)
    except ImportError:
        pass
    raw = (text or "").strip()
    if not raw:
        raise ValueError("模型返回为空")
    if raw.startswith("```"):
        lines = raw.splitlines()[1:]
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


def coerce_plan(raw: dict, total_seconds: float | None,
                style: str = "", source_text: str = "",
                library_names: list[Any] | None = None) -> dict:
    """归一化 Planner 输出（非法直接 ValueError）。

    total_seconds=None 时为 AI 定时长模式：total 取 AI 提议值
    （raw["total_seconds"]），并校验单镜表求和一致、提议值 ≥4s；
    显式传入时行为不变（以给定 total 为准）。
    去锚策略：auto 模式下缺镜表直接报错（不静默按 8s 补齐，fail fast
    触发上游 502 重试）；auto 模式下镜数≥3 全等凑整直接报错。
    显式 total 模式保留 8s 兜底与全等兼容（用户手工均匀切分合法）。
    人名 grounding（fail fast）：source_text 非空则 cast 名须在原文中
    逐字出现；library_names 非空则 cast 名须命中库名/别名（命中别名
    自动改写为库本名），违例均 ValueError；两者为空时保持旧透传行为。
    library_names 可传角色库条目 dict（含 name/aliases）或纯名Str。
    """
    if not isinstance(raw, dict):
        raise ValueError("Planner 返回须为 dict")
    auto_total = total_seconds is None or (
        isinstance(total_seconds, str) and not str(total_seconds).strip())
    if auto_total:
        raw_total = raw.get("total_seconds")
        if raw_total is None or (
                isinstance(raw_total, str) and not str(raw_total).strip()):
            raise ValueError("total_seconds 缺失（AI 须提议总时长）")
        try:
            total = float(raw_total)
        except (TypeError, ValueError):
            raise ValueError("total_seconds 须为数字") from None
        if total <= 0:
            raise ValueError(f"总时长须为正数，当前 {total}s")
        if total < MIN_CLIP:
            raise ValueError(f"总时长须 ≥{MIN_CLIP}s（单镜下限），当前 {total}s")
    else:
        try:
            total = float(total_seconds)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ValueError("total_seconds 须为数字") from None
        if total <= 0:
            raise ValueError(f"总时长须为正数，当前 {total}s")
    # --- clip_durations ---
    durs = raw.get("clip_durations")
    if durs is None:
        if auto_total:
            raise ValueError("clip_durations 缺失（AI 须按提议 total 自由切分，禁止省略）")
        durs = clip_durations_for_total(total)
    if not isinstance(durs, list) or not durs:
        raise ValueError("clip_durations 须为非空数组")
    norm_durs: list[Any] = []
    for d in durs:
        if isinstance(d, bool) or not isinstance(d, (int, float)):
            raise ValueError(f"clip_durations 须为数字，当前 {d!r}")
        norm_durs.append(d)
    if abs(sum(float(d) for d in norm_durs) - total) > 1e-6:
        raise ValueError(
            f"clip_durations 求和 {sum(float(d) for d in norm_durs)} "
            f"!= total_seconds={total}")
    for i, d in enumerate(norm_durs):
        f = float(d)
        last = i == len(norm_durs) - 1
        if not (MIN_CLIP <= f <= MAX_CLIP):
            raise ValueError(f"clip_durations[{i}]={d!r} 须在 4-12 内")
        if not last and not float(d).is_integer():
            raise ValueError(f"clip_durations[{i}]={d!r} 非末镜须为整数")
    if auto_total and len(norm_durs) >= 3:
        first = float(norm_durs[0])
        if all(abs(float(d) - first) < 1e-9 for d in norm_durs):
            raise ValueError("clip_durations 全等凑整（AI 须按剧情节奏混剪，镜数≥3 时须含至少两种不同秒数）")
    # --- style_effective ---
    style_eff = str(raw.get("style_effective", "") or "").strip() or \
        (style or "").strip() or "未指定"
    # --- cast_plan ---
    cast = raw.get("cast_plan", [])
    if cast is None:
        cast = []
    if not isinstance(cast, list):
        raise ValueError("cast_plan 须为数组")
    _ground_src = str(source_text or "")
    _lib: dict[str, str] | None = None
    if library_names:
        _alias_to_name: dict[str, str] = {}
        for _ln in library_names:
            if isinstance(_ln, dict):
                _nm0 = str(_ln.get("name", "") or "").strip()
                if _nm0:
                    _alias_to_name.setdefault(_nm0, _nm0)
                _als = _ln.get("aliases", [])
                for _a in (_als if isinstance(_als, list) else []):
                    _s0 = str(_a or "").strip()
                    if _s0:
                        _alias_to_name.setdefault(_s0, _nm0)
            else:
                _s0 = str(_ln or "").strip()
                if _s0:
                    _alias_to_name.setdefault(_s0, _s0)
        _lib = _alias_to_name
    if _ground_src or _lib is not None:
        _grounded: list[Any] = []
        for _entry in cast:
            if isinstance(_entry, dict):
                _nm = str(_entry.get("name", "") or "").strip()
                _rest: Any = _entry
            else:
                _nm = str(_entry or "").strip()
                _rest = {"name": _nm}
            if not _nm:
                raise ValueError("cast_plan 存在空角色名")
            if _ground_src and _nm not in _ground_src:
                raise ValueError(
                    f"cast_plan 角色 {_nm!r} 不在原文中（须为原文逐字原形）")
            if _lib is not None:
                if _nm not in _lib:
                    raise ValueError(
                        f"cast_plan 角色 {_nm!r} 不在角色库中"
                        "（库非空时只许用库名/别名）")
                _canon = _lib[_nm]
                if isinstance(_rest, dict) and _canon != _nm:
                    _rest = {**_rest, "name": _canon}
            _grounded.append(_rest)
        cast = _grounded
    # --- market ---
    market = raw.get("market")
    if not isinstance(market, dict):
        raise ValueError("market 缺失（须含 hook_3s/beats/cliffhanger）")
    hook = str(market.get("hook_3s", "") or "").strip()
    if not hook:
        raise ValueError("market.hook_3s 必填不可空")
    beats = market.get("beats")
    if not isinstance(beats, list) or not beats:
        raise ValueError("market.beats 须为非空数组")
    need = max(1, math.ceil(total / 15))
    if len(beats) < need:
        raise ValueError(f"market.beats 至少 {need} 个（每15s至少1个），当前 {len(beats)}")
    cliff = str(market.get("cliffhanger", "") or "").strip()
    if not cliff:
        raise ValueError("market.cliffhanger 必填（结尾必留悬念）")
    try:
        b15 = float(market.get("beats_per_15s", len(beats) / (total / 15)))
    except (TypeError, ValueError):
        b15 = round(len(beats) / (total / 15), 3)
    # --- density ---
    density = raw.get("density")
    if not isinstance(density, dict):
        density = {}
    try:
        cps = float(density.get("chars_per_sec", 10.0))
    except (TypeError, ValueError):
        raise ValueError("density.chars_per_sec 须为数字") from None
    if not (8 <= cps <= 14):
        raise ValueError(f"density.chars_per_sec 须在 8-14 内，当前 {cps}")
    try:
        dmax = int(density.get("dialogue_max", 30))
    except (TypeError, ValueError):
        raise ValueError("density.dialogue_max 须为整数") from None
    if dmax <= 0:
        raise ValueError(f"density.dialogue_max 须为正整数，当前 {dmax}")
    if dmax > 30:
        raise ValueError(f"density.dialogue_max<=30，当前 {dmax}")
    return {
        "total_seconds": total,
        "clip_durations": norm_durs,
        "style_effective": style_eff,
        "cast_plan": cast,
        "market": {
            "hook_3s": hook,
            "beats": beats,
            "beats_per_15s": round(float(b15), 3),
            "cliffhanger": cliff,
            "risk": str(market.get("risk", "") or ""),
        },
        "density": {"chars_per_sec": cps, "dialogue_max": dmax},
    }


def validate_plan(plan: dict) -> bool:
    """校验 Planner 输出，通过返回 True，否则 ValueError（兼容 AI 提议 total）。"""
    if not isinstance(plan, dict):
        raise ValueError("Planner 返回须为 dict")
    tot = plan.get("total_seconds", None)
    if tot is None or (isinstance(tot, str) and not str(tot).strip()):
        coerce_plan(plan, None,
                    str(plan.get("style_effective", "") or ""))
    else:
        coerce_plan(plan, float(tot),
                    str(plan.get("style_effective", "") or ""))
    return True
