"""场景库（与角色库平行；分镜 reference/keyframe 资产）。

``scenes.json`` schema（存于 ``outputs/<系列>/scenes.json``）::

    {
      "scenes": [
        {
          "id": "s01",
          "name": "雪夜山门",
          "description": "场景描述（<=200字，生图/分镜注入用）",
          "images": ["scenes/s01_20260101-120000.png"],
          "first_seen": "E01",
          "notes": "备注",
          "locked": false
        }
      ],
      "updated_at": "..."
    }

- 手动建 + 上传为主；AI extract 为可选（分镜 cast/scene 缺图时提示补）。
- ``description`` <= 200 字（超长直接 ValueError，不静默截断）。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from series import scenes_path as _scenes_path  # type: ignore
except ImportError:
    try:
        from backend.series import scenes_path as _scenes_path  # type: ignore
    except ImportError:
        _scenes_path = None  # type: ignore

DESCRIPTION_MAX = 200


def _parse_locked(v: object) -> bool:
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


def _strict_key(s: str) -> str:
    return str(s or "").strip()


def new_scene_id(existing: list[dict]) -> str:
    nums: list[int] = []
    for c in existing or []:
        if not isinstance(c, dict):
            continue
        m = re.match(r"^s(\d+)$", str(c.get("id", "") or "").strip().lower())
        if m:
            try:
                nums.append(int(m.group(1)))
            except ValueError:
                continue
    return f"s{(max(nums) + 1) if nums else 1:02d}"


def normalize_scene(raw: dict, default_id: str = "") -> dict:
    if not isinstance(raw, dict):
        raise ValueError("场景须为 dict")
    name = str(raw.get("name", "") or "").strip()
    if not name:
        raise ValueError("场景 name 不能为空")
    images: list[str] = []
    for im in (raw.get("images", []) or []):
        s = str(im or "").strip()
        if s and s not in images:
            images.append(s)
    c: dict[str, Any] = {
        "id": str(raw.get("id", "") or default_id or "").strip(),
        "name": name,
        "description": str(raw.get("description", "") or "").strip(),
        "images": images,
        "first_seen": str(raw.get("first_seen", "") or "").strip(),
        "notes": str(raw.get("notes", "") or "").strip(),
        "locked": _parse_locked(raw.get("locked", False)),
    }
    if not c["id"]:
        raise ValueError(f"场景 {name!r} 缺少 id")
    return c


def validate_scene(c: dict) -> bool:
    if not isinstance(c, dict):
        raise ValueError("场景须为 dict")
    if not str(c.get("id", "") or "").strip():
        raise ValueError("场景 id 不能为空")
    if not str(c.get("name", "") or "").strip():
        raise ValueError("场景 name 不能为空")
    if len(str(c.get("description", "") or "")) > DESCRIPTION_MAX:
        raise ValueError(
            f"场景 {c.get('name')!r} description 超 {DESCRIPTION_MAX} 字")
    if not isinstance(c.get("images", []), list):
        raise ValueError("images 须为数组")
    if not isinstance(c.get("locked", False), bool):
        raise ValueError("locked 须为布尔")
    return True


def scene_keys(c: dict) -> set[str]:
    """场景名严格键集合（strip 精确，大小写敏感）。"""
    keys: set[str] = set()
    name = _strict_key(c.get("name", ""))
    if name:
        keys.add(name)
    return keys


def merge_scenes(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """增量合并：同名命中只并入 images、描述只填空不覆盖；新场景追加。"""
    base: list[dict] = []
    for c in (existing or []):
        if isinstance(c, dict) and str(c.get("name", "") or "").strip():
            try:
                cc = normalize_scene(c)
                validate_scene(cc)
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
            norm = normalize_scene({**raw,
                                    "id": str(raw.get("id", "") or "").strip()
                                    or new_scene_id(base)})
        except ValueError:
            continue
        hit_idx = -1
        for i, cur in enumerate(base):
            if str(cur.get("name", "") or "").strip() == name:
                hit_idx = i
                break
        if hit_idx < 0:
            ids = {str(c.get("id")) for c in base}
            if norm["id"] in ids:
                norm["id"] = new_scene_id(base)
            try:
                validate_scene(norm)
            except ValueError:
                continue
            base.append(norm)
            continue
        cur = base[hit_idx]
        for im in norm["images"]:
            if im and im not in cur["images"]:
                cur["images"].append(im)
        for field in ("description", "notes"):
            if not str(cur.get(field, "") or "").strip():
                v = str(norm.get(field, "") or "").strip()
                if v:
                    cur[field] = v
        if not str(cur.get("first_seen", "") or "").strip():
            v = str(norm.get("first_seen", "") or "").strip()
            if v:
                cur["first_seen"] = v
        if norm.get("locked") and not cur.get("locked"):
            cur["locked"] = True
        try:
            validate_scene(cur)
        except ValueError:
            continue
        base[hit_idx] = cur
    return base


def summarize_scenes(items: list[dict], limit: int = 2000) -> str:
    """场景摘要（供分镜 prompt 注入：名+描述+有图标记）。"""
    libs = [c for c in (items or []) if isinstance(c, dict)
            and str(c.get("name", "") or "").strip()]
    if not libs:
        return ""
    try:
        lim = int(limit)
    except (TypeError, ValueError):
        lim = 2000
    lines: list[str] = []
    for c in libs:
        name = str(c.get("name", "") or "").strip()
        desc = str(c.get("description", "") or "").strip()
        imgs = c.get("images", [])
        mark = "有图" if isinstance(imgs, list) and any(
            str(x or "").strip() for x in imgs) else "无图"
        seg = f"- {name}（{mark}）"
        if desc:
            seg += f"：{desc[:80]}"
        lines.append(seg)
    text = "\n".join(lines).strip()
    if len(text) <= lim:
        return text
    total = len(lines)
    kept: list[str] = []
    for line in lines:
        footer = f"\n…（共 {total} 个场景，已列 {len(kept) + 1} 个）"
        if len("\n".join(kept + [line])) + len(footer) <= lim:
            kept.append(line)
        else:
            break
    if not kept:
        return f"…（共 {total} 个场景，已列 0 个）"[:lim]
    return "\n".join(kept) + f"\n…（共 {total} 个场景，已列 {len(kept)} 个）"


def scene_asset_lines(items: list[dict]) -> str:
    """可用资产清单行：有图才列主图路径（供 AI 抄写引用，不编造）。"""
    lines: list[str] = []
    for c in (items or []):
        if not isinstance(c, dict):
            continue
        name = str(c.get("name", "") or "").strip()
        if not name:
            continue
        imgs = c.get("images", [])
        main = ""
        if isinstance(imgs, list):
            for im in imgs:
                s = str(im or "").strip()
                if s:
                    main = s
                    break
        if main:
            lines.append(f"- {name}：{main}")
        else:
            lines.append(f"- {name}（无图）")
    return "\n".join(lines).strip()


def select_scene_refs(items: list[dict],
                      scene_names: list[str] | None = None,
                      limit: int = 5) -> list[str]:
    """为 script.scene_refs 选取场景主图（每镜引用上限，库本身不限）。

    排序：本集出场（scene_names 命中）优先，其余按库序；每场景取主图
    images[0]（无图跳过），去重后截断 limit。
    """
    libs = [c for c in (items or []) if isinstance(c, dict)]
    order: dict[str, int] = {}
    for n in (scene_names or []):
        k = str(n or "").strip()
        if k and k not in order:
            order[k] = len(order)
    key_to_idx: dict[str, int] = {}
    for i, c in enumerate(libs):
        k = str(c.get("name", "") or "").strip()
        if k:
            key_to_idx.setdefault(k, i)

    def _score(i: int) -> tuple[int, int]:
        c = libs[i]
        k = str(c.get("name", "") or "").strip()
        if k in order:
            return (0, order[k])
        return (1, i)

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


def load_scenes(series_name: str,
                base_dir: Optional[str | Path] = None) -> list[dict]:
    if _scenes_path is None:
        raise RuntimeError("series 模块缺失，无法定位 scenes.json")
    p = _scenes_path(series_name, base_dir)
    if not p.is_file():
        return []
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(obj, dict) and isinstance(obj.get("scenes"), list):
        return [c for c in obj["scenes"] if isinstance(c, dict)]
    if isinstance(obj, list):
        return [c for c in obj if isinstance(c, dict)]
    return []


def save_scenes(series_name: str, items: list[dict],
                base_dir: Optional[str | Path] = None) -> list[dict]:
    if _scenes_path is None:
        raise RuntimeError("series 模块缺失，无法定位 scenes.json")
    for c in items or []:
        validate_scene(c)
    p = _scenes_path(series_name, base_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"scenes": list(items or []), "updated_at": _now_iso()}
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                 encoding="utf-8")
    return list(items or [])


def build_scene_prompt(scene: dict, style: str = "",
                       ratio: str = "9:16") -> str:
    """用 description 拼六段式生图 prompt（场景图通道，size 固定 1K）。"""
    name = str(scene.get("name", "") or "").strip() or "场景"
    desc = str(scene.get("description", "") or "").strip() or "电影感场景"
    if len(desc) > DESCRIPTION_MAX:
        raise ValueError(f"description 超 {DESCRIPTION_MAX} 字，拒绝生图")
    style_seg = (style or "").strip() or "电影感写实"
    composition = "竖构图" if ratio != "16:9" else "横构图"
    return (f"[主体]{name}，{desc}+[场景]{desc}+[风格]{style_seg}"
            f"+[光照]电影光+[构图]{composition}+[质量]1K,高细节")
