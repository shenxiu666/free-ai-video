"""角色表（Builder M1；docs/04.5 全局角色锚点）。

``characters.json`` schema（存于 ``outputs/<剧>/characters.json``）::

    {
      "characters": [
        {
          "id": "c01",
          "name": "阿雪",
          "aliases": ["雪姑娘"],
          "logline": "一句话小传",
          "appearance": "外貌（<=200字）",
          "personality": "性格",
          "relation": "关系/阵营",
          "outfit": "服装特征",
          "status": "待确认/已确认（自动识别默认 待确认）",
          "images": ["characters/c01_20260101-120000.png"],
          "voice": {"provider": "edge-tts", "voice_id": "zh-CN-XiaoxiaoNeural"},
          "first_seen": "E01",
          "notes": "备注",
          "locked": false
        }
      ],
      "updated_at": "..."
    }

- LLM 抽取 + 增量合并：手改 ``locked=true`` 的条目不被覆盖（仅并入新别名/立绘）；
  同名/别名命中即合并（大小写/空白归一后交集非空），否则追加。
- ``appearance`` <= 200 字（超长直接 ValueError，不静默截断）。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from series import characters_path as _characters_path  # type: ignore
except ImportError:
    try:
        from backend.series import characters_path as _characters_path  # type: ignore
    except ImportError:
        _characters_path = None  # type: ignore

APPEARANCE_MAX = 200


def _parse_locked(v: object) -> bool:
    """显式解析布尔：仅 True/1/\"true\"（大小写不敏感）为真。

    防 ``locked="false"`` 字符串被 ``bool()`` 误判为 True。
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and not isinstance(v, bool):
        return v == 1
    if isinstance(v, float):
        return v == 1.0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true")
    return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_key(s: str) -> str:
    """严格键：大小写敏感、strip 后比对，不做模糊/包含匹配。

    历史实现为 lower+去全空白归一（会把 “Hally/hally”、“阿 雪/阿雪”
    误判为同一角色而硬并）；G2 起收紧为 strip 精确比对，宁可留两条
    待用户手动合。保留函数名以兼容旧导入，语义已从严。
    """
    return str(s or "").strip()


def _strict_key(s: str) -> str:
    """strip 精确键（大小写敏感），与 _norm_key 同义，语义显式处用此名。"""
    return str(s or "").strip()


def char_keys(c: dict) -> set[str]:
    """名/别名严格键集合（strip 精确，大小写敏感）。"""
    keys: set[str] = set()
    name = _strict_key(c.get("name", ""))
    if name:
        keys.add(name)
    aliases = c.get("aliases", [])
    if isinstance(aliases, list):
        for a in aliases:
            k = _strict_key(a)
            if k:
                keys.add(k)
    return keys


def new_character_id(existing: list[dict]) -> str:
    nums: list[int] = []
    for c in existing or []:
        if not isinstance(c, dict):
            continue
        m = re.match(r"^c(\d+)$", str(c.get("id", "")).strip().lower())
        if m:
            try:
                nums.append(int(m.group(1)))
            except ValueError:
                continue
    return f"c{(max(nums) + 1) if nums else 1:02d}"


def normalize_character(raw: dict, default_id: str = "") -> dict:
    """填充默认值并 trim（不做跨条目合并，不截断 appearance）。"""
    if not isinstance(raw, dict):
        raise ValueError("角色须为 dict")
    name = str(raw.get("name", "") or "").strip()
    if not name:
        raise ValueError("角色 name 不能为空")
    aliases: list[str] = []
    for a in (raw.get("aliases", []) or []):
        s = str(a or "").strip()
        if s and s not in aliases and s != name:
            aliases.append(s)
    images: list[str] = []
    for im in (raw.get("images", []) or []):
        s = str(im or "").strip()
        if s and s not in images:
            images.append(s)
    voice = raw.get("voice", {})
    if not isinstance(voice, dict):
        voice = {}
    c: dict[str, Any] = {
        "id": str(raw.get("id", "") or default_id or "").strip(),
        "name": name,
        "aliases": aliases,
        "logline": str(raw.get("logline", "") or "").strip(),
        "appearance": str(raw.get("appearance", "") or "").strip(),
        "personality": str(raw.get("personality", "") or "").strip(),
        "relation": str(raw.get("relation", "") or "").strip(),
        "outfit": str(raw.get("outfit", "") or "").strip(),
        # 自动识别/占位默认 待确认（空即回填，手改 已确认 由 merge 粘性保留）
        "status": str(raw.get("status", "") or "").strip() or "待确认",
        "images": images,
        "voice": {
            "provider": str(voice.get("provider", "") or "").strip(),
            "voice_id": str(voice.get("voice_id", "") or "").strip(),
        },
        "first_seen": str(raw.get("first_seen", "") or "").strip(),
        "notes": str(raw.get("notes", "") or "").strip(),
        "locked": _parse_locked(raw.get("locked", False)),
        "is_main": _parse_locked(raw.get("is_main", False)),
    }
    if not c["id"]:
        raise ValueError(f"角色 {name!r} 缺少 id")
    return c


def validate_character(c: dict) -> bool:
    if not isinstance(c, dict):
        raise ValueError("角色须为 dict")
    if not str(c.get("id", "") or "").strip():
        raise ValueError("角色 id 不能为空")
    if not str(c.get("name", "") or "").strip():
        raise ValueError("角色 name 不能为空")
    if len(str(c.get("appearance", "") or "")) > APPEARANCE_MAX:
        raise ValueError(
            f"角色 {c.get('name')!r} appearance 超 {APPEARANCE_MAX} 字"
            f"（当前 {len(str(c.get('appearance') or ''))} 字）")
    if not isinstance(c.get("aliases", []), list):
        raise ValueError("aliases 须为数组")
    if not isinstance(c.get("images", []), list):
        raise ValueError("images 须为数组")
    voice = c.get("voice", {})
    if not isinstance(voice, dict):
        raise ValueError("voice 须为对象")
    if not isinstance(c.get("locked", False), bool):
        raise ValueError("locked 须为布尔")
    if not isinstance(c.get("is_main", False), bool):
        raise ValueError("is_main 须为布尔")
    # 新字段：缺键/空兼容老记录（normalize 回填默认），显式非法才拒
    _rel = c.get("relation", "")
    if _rel is None:
        _rel = ""
    if not isinstance(_rel, str):
        raise ValueError("relation 须为字符串")
    _out = c.get("outfit", "")
    if _out is None:
        _out = ""
    if not isinstance(_out, str):
        raise ValueError("outfit 须为字符串")
    _st = c.get("status", "")
    if _st is None or _st == "":
        pass
    elif _st not in ("待确认", "已确认"):
        raise ValueError(
            f"角色 {c.get('name')!r} status 非法：{c.get('status')!r}"
            "（仅允许 待确认/已确认）")
    return True


def build_character_prompt(source_text: str,
                           existing_summary: str = "") -> tuple[str, str]:
    src = (source_text or "").strip()
    truncated = len(src) > 12000
    if truncated:
        src = src[:12000]
    system = ("你是短剧角色抽取师（只做抽取不编造）。只输出一个合法 JSON 对象，"
              "不要输出任何解释、前后缀与 Markdown 围栏。所有字符串用中文。")
    user = f"""从下面的原文中抽取主要角色（原文主要角色全部列出，不设数量上限），输出严格 JSON：

【输出结构】
{{
  "characters": [
    {{
      "name": "角色名（必填）",
      "aliases": ["别名/尊称"],
      "logline": "一句话小传",
      "appearance": "外貌（<=200字，立绘用）",
      "personality": "性格",
      "relation": "关系/阵营",
      "outfit": "服装特征",
      "status": "待确认",
      "voice": {{"provider": "", "voice_id": ""}},
      "first_seen": "首次出场（如 第一章）",
      "notes": "备注"
    }}
  ]
}}

 【硬规则】
1. 只收录原文明确出现的角色，不编造；appearance<=200字。
2. name 必须是原文逐字原形（原文中逐字出现的写法），禁音译/改写/翻译/拉丁转写；
   同一人多写法并存时 name 取原文中出现次数最多的原形，其余写法进 aliases。
3. relation/outfit 无明确信息可空；status 一律填"待确认"。
"""
    if existing_summary and str(existing_summary).strip():
        user += f"\n【已有角色（同名/别名即合并，不重建）】\n{str(existing_summary).strip()}\n"
    user += f"\n【原文】{'（过长已截断前 12000 字）' if truncated else ''}\n{src}"
    return system, user


def coerce_characters_with_skipped(raw: dict,
                                     first_seen: str = ""
                                     ) -> tuple[list[dict], int]:
    """把 LLM JSON 归一化为角色列表（库不限量；单条容错）。

    单条 normalize/validate 失败只跳过并计入 skipped（防输出过长截断拖死整批）；
    全部无效才 raise ValueError。返回 (角色列表, 跳过条数)。
    """
    items = raw.get("characters") if isinstance(raw, dict) else None
    if not isinstance(items, list) or not items:
        raise ValueError("模型返回缺少 characters 数组")
    out: list[dict] = []
    skipped = 0
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            skipped += 1
            continue
        cid = str(item.get("id", "") or f"c{i + 1:02d}").strip()
        try:
            c = normalize_character({**item, "id": cid})
            if first_seen and not c["first_seen"]:
                c["first_seen"] = first_seen
            validate_character(c)
        except ValueError:
            skipped += 1
            continue
        out.append(c)
    if not out:
        raise ValueError(
            f"模型返回的 {len(items)} 条角色均无效（已跳过 {skipped} 条）")
    return out, skipped


def coerce_characters(raw: dict, first_seen: str = "") -> list[dict]:
    """把 LLM JSON 归一化为角色列表（id 暂按 c01… 顺排，合并时再重排）。

    单条失败跳过（见 coerce_characters_with_skipped）；兼容旧调用只返列表。
    """
    out, _ = coerce_characters_with_skipped(raw, first_seen)
    return out


def canonicalize_names(items: list[dict],
                       source_text: str = "") -> tuple[list[dict], int]:
    """G1 逐字 grounding：把每条的 name/aliases 对齐到原文原形。

    对每条取 ``{name}+aliases`` 在原文中的出现次数（``str.count`` 子串
    计数，大小写敏感），最高频原形为 name（并列取原 name），其余非空
    异形进 aliases 去重保序；name 与 aliases 在原文中一次都找不到 →
    该条 skipped（计数，不进库）。

    返回 (grounded列表, skipped条数)。输入条目不原地修改，返回浅拷贝。
    """
    src = source_text or ""
    kept: list[dict] = []
    skipped = 0
    for raw in (items or []):
        if not isinstance(raw, dict):
            skipped += 1
            continue
        orig_name = str(raw.get("name", "") or "").strip()
        aliases = raw.get("aliases", [])
        if not isinstance(aliases, list):
            aliases = []
        cands: list[str] = []
        for s in [orig_name] + [str(a or "").strip() for a in aliases]:
            ss = str(s or "").strip()
            if ss and ss not in cands:
                cands.append(ss)
        if not cands:
            skipped += 1
            continue
        counts: list[tuple[str, int]] = [
            (c, (src.count(c) if src else 0)) for c in cands
        ]
        best_n = max((n for _, n in counts), default=0)
        if best_n <= 0:
            skipped += 1
            continue
        orig_n = src.count(orig_name) if (orig_name and src) else 0
        if orig_n == best_n and orig_name:
            best = orig_name
        else:
            best = next(c for c, n in counts if n == best_n)
        new_aliases = [c for c in cands if c != best]
        out = dict(raw)
        out["name"] = best
        out["aliases"] = new_aliases
        kept.append(out)
    return kept, skipped


def _ep_num(value: str) -> int | None:
    """解析 E01/E1/1 为集号（比大小用），解析不出返回 None。"""
    import re as _re

    s = str(value or "").strip().upper()
    m = _re.match(r"^E?(\d+)$", s)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _earliest_first_seen(cur: str, new: str) -> str:
    """first_seen 取最早出场集：只提前、不后移；空值由非空补齐。"""
    c = str(cur or "").strip()
    n = str(new or "").strip()
    if not c:
        return n
    if not n:
        return c
    cn, nn = _ep_num(c), _ep_num(n)
    if cn is not None and nn is not None:
        return c if nn >= cn else n
    return c


def merge_characters(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """增量合并（有新增就增加，没有就不变）：同名/别名命中时只并入
    新别名/立绘，描述字段只填空不覆盖，已有内容永不被自动改写
    （手动改走 PATCH+locked）；first_seen 恒为最早集；locked/已确认
    粘性保持；新角色追加（status 待确认）。自动流程永不删、不改名。

    G2 从严：命中仅三者之一（大小写敏感、strip 后比对，不做模糊/包含
    匹配）——name 相等 / 别名交集非空 / 一方 name 在另一方 aliases 中；
    宁可留两条待用户手动合，不自动硬并。locked/已确认粘性保持。
    """
    base: list[dict] = []
    for c in (existing or []):
        if isinstance(c, dict) and str(c.get("name", "") or "").strip():
            try:
                cc = normalize_character(c)
                validate_character(cc)
                base.append(cc)
            except ValueError:
                continue
    for raw in (incoming or []):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", "") or "").strip()
        if not name:
            continue
        try:
            norm = normalize_character({**raw,
                                        "id": str(raw.get("id", "") or "").strip()
                                        or new_character_id(base)})
        except ValueError:
            continue
        # 找命中（G2 从严，大小写敏感、strip 后精确比对）：
        # name 相等 / 别名交集非空 / 一方 name 在另一方 aliases 中，三者之一。
        hit_idx = -1
        n_name = str(norm.get("name", "") or "").strip()
        n_alias = [str(a or "").strip() for a in (norm.get("aliases", []) or [])
                   if str(a or "").strip()]
        n_alias_set = set(n_alias)
        for i, cur in enumerate(base):
            c_name = str(cur.get("name", "") or "").strip()
            c_alias = [str(a or "").strip() for a in (cur.get("aliases", []) or [])
                       if str(a or "").strip()]
            c_alias_set = set(c_alias)
            if n_name and n_name == c_name:
                hit_idx = i
                break
            if n_alias_set & c_alias_set:
                hit_idx = i
                break
            if (n_name and n_name in c_alias_set) or (c_name and c_name in n_alias_set):
                hit_idx = i
                break
        if hit_idx < 0:
            # 防 id 冲突
            ids = {str(c.get("id")) for c in base}
            if norm["id"] in ids:
                norm["id"] = new_character_id(base)
            try:
                validate_character(norm)
            except ValueError:
                continue
            base.append(norm)
            continue
        cur = base[hit_idx]
        # 加法合并（有新增就增加，没有就不变）：命中已存在角色时只并入
        # 别名/立绘（去重保序）；描述字段只填空不覆盖（空占位可被后来的
        # 完整抽取补齐，但已有内容永不被自动改写；手动改走 PATCH+locked）。
        for a in norm["aliases"]:
            if a and a not in cur["aliases"] and a != cur["name"]:
                cur["aliases"].append(a)
        for im in norm["images"]:
            if im and im not in cur["images"]:
                cur["images"].append(im)
        for field in ("logline", "appearance", "personality", "notes",
                      "relation", "outfit"):
            if not str(cur.get(field, "") or "").strip():
                v = str(norm.get(field, "") or "").strip()
                if v:
                    cur[field] = v
        # first_seen 恒为最早出场集（只提前、不后移）
        cur["first_seen"] = _earliest_first_seen(
            cur.get("first_seen", ""), norm.get("first_seen", ""))
        # status：已确认粘性——不被自动识别的 待确认 回退覆盖；
        # 新条目要求 已确认 则允许升级。
        _ns = str(norm.get("status", "") or "").strip()
        if _ns == "已确认":
            cur["status"] = "已确认"
        elif _ns and not str(cur.get("status", "") or "").strip():
            cur["status"] = _ns
        # voice：只填空子字段，不覆盖已有
        nv = norm.get("voice", {}) or {}
        if isinstance(nv, dict):
            for k in ("provider", "voice_id"):
                if not str((cur.get("voice", {}) or {}).get(k, "") or "").strip():
                    vv = str(nv.get(k, "") or "").strip()
                    if vv:
                        cur.setdefault("voice", {})[k] = vv
        # locked：一旦上锁保持（不自动解锁）；新条目要求上锁则允许
        if norm.get("locked") and not cur.get("locked"):
            cur["locked"] = True
        try:
            validate_character(cur)
        except ValueError:
            continue
        base[hit_idx] = cur
    return base


def summarize_characters(chars: list[dict], limit: int = 2000) -> str:
    """角色摘要（供 Planner/分镜 prompt 注入：名+外貌+性格）。

    预算语义（limit 为字符数上限）：
    1. 全量详版（名+别名+外貌+性格）能放下 → 原样返回，不加尾注；
    2. 放不下 → 降级为每角色一行紧凑版（name+首图标记+一句话，
       一句话取 logline>appearance>personality，超 80 字截断），全员能
       放下则返回紧凑版（不截断、不加尾注）；
    3. 紧凑版仍超长 → 按序截断，保证已列行完整，并在尾部追加
       ``…（共 N 个角色，已列 M 个）``（尾注计入 limit 内）。
    即 limit 再小也优先保证“每角色至少一行”，而不是旧逻辑的无声
    前 limit 截断（会丢掉后半角色）。注入处（main plan/breakdown/
    extract）直接用返回值即可，无需再截断。
    """
    items: list[dict] = []
    for c in (chars or []):
        if not isinstance(c, dict):
            continue
        name = str(c.get("name", "") or "").strip()
        if not name:
            continue
        items.append(c)
    if not items:
        return ""
    try:
        lim = int(limit)
    except (TypeError, ValueError):
        lim = 2000
    if lim <= 0:
        return f"…（共 {len(items)} 个角色，已列 0 个）"
    lines: list[str] = []
    for c in items:
        name = str(c.get("name", "") or "").strip()
        aliases = ", ".join(c.get("aliases", []) or [])
        appearance = str(c.get("appearance", "") or "").strip()
        personality = str(c.get("personality", "") or "").strip()
        seg = f"- {name}" + (f"（又名{aliases}）" if aliases else "")
        if appearance:
            seg += f"：{appearance}"
        if personality:
            seg += f"；性格{personality}"
        lines.append(seg)
    text = "\n".join(lines).strip()
    if len(text) <= lim:
        return text
    # 降级：每角色一行紧凑版
    compact: list[str] = []
    for c in items:
        name = str(c.get("name", "") or "").strip()
        imgs = c.get("images", [])
        has_img = isinstance(imgs, list) and any(
            str(x or "").strip() for x in imgs)
        one = (str(c.get("logline", "") or "").strip()
               or str(c.get("appearance", "") or "").strip()
               or str(c.get("personality", "") or "").strip())
        one = " ".join(str(one).split())
        if len(one) > 80:
            one = one[:80] + "…"
        mark = "有图" if has_img else "无图"
        seg = f"- {name}（{mark}）"
        if one:
            seg += f"：{one}"
        compact.append(seg)
    total = len(compact)
    compact_text = "\n".join(compact).strip()
    if len(compact_text) <= lim:
        return compact_text
    # 仍超长：按序截断 + 尾注（尾注计入限额，至少保留尾注）
    kept: list[str] = []
    for line in compact:
        trial = len("\n".join(kept + [line]))
        footer = f"\n…（共 {total} 个角色，已列 {len(kept) + 1} 个）"
        if trial + len(footer) <= lim:
            kept.append(line)
        else:
            break
    if not kept:
        footer = f"…（共 {total} 个角色，已列 0 个）"
        return compact[0][:max(0, lim - len(footer) - 1)] + "…" + footer \
            if lim > len(footer) else footer[:lim]
    return "\n".join(kept) + f"\n…（共 {total} 个角色，已列 {len(kept)} 个）"


def select_character_refs(chars: list[dict],
                            cast_names: list[str] | None = None,
                            limit: int = 5) -> list[str]:
    """为 script.character_refs 选取角色主图（每镜引用上限，库本身不限）。

    排序：本集出场（cast_names 命中，别名归一）优先、其中 is_main 居前；
    其余按库序；每角色取主图 images[0]（无图跳过），去重后截断 limit。
    """
    libs = [c for c in (chars or []) if isinstance(c, dict)]
    order: dict[str, int] = {}
    for n in (cast_names or []):
        k = _norm_key(n)
        if k and k not in order:
            order[k] = len(order)
    # 建键→下标（名/别名归一，首个占位）
    key_to_idx: dict[str, int] = {}
    for i, c in enumerate(libs):
        for k in char_keys(c):
            key_to_idx.setdefault(k, i)

    def _score(i: int) -> tuple[int, int, int]:
        c = libs[i]
        keys = char_keys(c)
        hits = [order[k] for k in keys if k in order]
        in_cast = min(hits) if hits else 10 ** 9
        main = 0 if _parse_locked(c.get("is_main", False)) else 1
        return (0 if hits else 1, main if hits else 1, in_cast if hits else i)

    ranked = sorted(range(len(libs)), key=_score)
    refs: list[str] = []
    for i in ranked:
        imgs = libs[i].get("images", [])
        if not isinstance(imgs, list):
            continue
        for im in imgs:
            s = str(im or "").strip()
            if s:
                if s not in refs:
                    refs.append(s)
                break
        if len(refs) >= max(1, int(limit)):
            break
    return refs[:max(1, int(limit))]


def load_characters(series_name: str,
                    base_dir: Optional[str | Path] = None) -> list[dict]:
    if _characters_path is None:
        raise RuntimeError("series 模块缺失，无法定位 characters.json")
    p = _characters_path(series_name, base_dir)
    if not p.is_file():
        return []
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(obj, dict) and isinstance(obj.get("characters"), list):
        out = []
        for c in obj["characters"]:
            if isinstance(c, dict):
                out.append(c)
        return out
    if isinstance(obj, list):
        return [c for c in obj if isinstance(c, dict)]
    return []


def save_characters(series_name: str, chars: list[dict],
                    base_dir: Optional[str | Path] = None) -> list[dict]:
    if _characters_path is None:
        raise RuntimeError("series 模块缺失，无法定位 characters.json")
    for c in chars or []:
        validate_character(c)
    p = _characters_path(series_name, base_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"characters": list(chars or []),
               "updated_at": _now_iso()}
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                 encoding="utf-8")
    return list(chars or [])


def build_portrait_prompt(character: dict, style: str = "",
                          ratio: str = "9:16") -> str:
    """用 appearance 拼六段式生图 prompt（立绘通道，size 固定 1K）。

    六段式：[主体+场景+风格+光照+构图+质量]；主体必含名+外貌。
    """
    name = str(character.get("name", "") or "").strip() or "角色"
    appearance = str(character.get("appearance", "") or "").strip() or "五官端正"
    if len(appearance) > APPEARANCE_MAX:
        raise ValueError(f"appearance 超 {APPEARANCE_MAX} 字，拒绝生图")
    personality = str(character.get("personality", "") or "").strip()
    subject = f"{name}，{appearance}" + (f"，{personality}" if personality else "")
    scene = "纯色背景人物立绘，全身"
    style_seg = (style or "").strip() or "电影感写实"
    lighting = "柔光正面光"
    composition = "竖构图全身" if ratio != "16:9" else "横构图半身"
    return (f"[主体]{subject}+[场景]{scene}+[风格]{style_seg}"
            f"+[光照]{lighting}+[构图]{composition}+[质量]1K,高细节")
