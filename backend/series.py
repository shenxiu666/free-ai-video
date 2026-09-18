"""系列/分集存储（Builder M1；docs/01.6、docs/04）。

布局（outputs/<剧>/ 为系列根，与 orchestrator 单剧目录复用，但冲突时
系列创建选 400 提示重名方案、不自动隔离；删除系列只删 series.json +
episodes/ + characters.json，老单剧 script.json/state.json 一律保留）：

```text
outputs/<剧>/
  series.json            # 系列元数据 + 分集索引 + total_seconds（各集之和）
  characters.json        # 角色表（characters.py 读写，本模块只保证占位）
  episodes/
    E01/
      meta.json          # 分集元数据（id/title/total_seconds/source_*）
      source.md          # 原文（超 12000 字截断前部，记 truncated）
      plan.json          # Planner 输出（planner.py）
      script.json        # 分镜初稿（breakdown.py，经 validate_script 校验）
      state.json         # 渲染状态（orchestrator 形，可选）
    E02/ ...
```

- 剧名净化复用 orchestrator._drama_dir 逻辑：过滤 ``\\/:*?"<>|`` 并 strip，
  为空 raise ValueError；返回目录均经该净化，防路径穿越。
- 系列总时长 = 各集 total_seconds 之和（get 时实时重算并回写）。
- 本模块为纯函数 + 文件 IO，不依赖 FastAPI；``base_dir`` 供测试注入
  （None 则为仓库 outputs/）。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SOURCE_MAX_CHARS = 12000  # 与 breakdown.SOURCE_MAX_CHARS 同值（超长截断前部）
EP_ID_RE = re.compile(r"^E(\d+)$", re.IGNORECASE)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _outputs_root(base_dir: Optional[str | Path] = None) -> Path:
    if base_dir is not None:
        return Path(base_dir)
    return Path(__file__).resolve().parent.parent / "outputs"


def sanitize_series_name(name: str) -> str:
    """剧名净化（与 orchestrator._drama_dir 同逻辑 + 防穿越）。

    过滤 ``\\/:*?"<>|`` 并 strip，为空 raise ValueError；
    纯 ``.``/``..``/``...``（仅点号组成）一律拒绝，防 ``outputs/..`` 越界。
    """
    safe = "".join(c for c in (name or "") if c not in '\\/:*?"<>|').strip()
    if not safe:
        raise ValueError("剧名不能为空")
    if set(safe) == {"."}:
        raise ValueError(f"剧名非法（纯 ./../...）：{name!r}")
    return safe


def series_dir(name: str, base_dir: Optional[str | Path] = None) -> Path:
    root = _outputs_root(base_dir)
    safe = sanitize_series_name(name)
    target = root / safe
    # resolve 后断言在 outputs_root 内（防 .. 穿越；strict=False 兼容未建目录）
    try:
        r = root.resolve()
        t = target.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"剧名越界：{name!r}") from exc
    if t == r:
        raise ValueError(f"剧名非法（指向根目录）：{name!r}")
    try:
        t.relative_to(r)
    except ValueError as exc:
        raise ValueError(f"剧名越界：{name!r}") from exc
    return target


def episodes_dir(name: str, base_dir: Optional[str | Path] = None) -> Path:
    return series_dir(name, base_dir) / "episodes"


def normalize_episode_id(ep: str) -> str:
    """接受 1/01/E1/e01/E01（含更大集数），统一为 E + 至少两位零填充。"""
    s = str(ep or "").strip().upper()
    if not s:
        raise ValueError("分集 id 不能为空")
    if s.startswith("E"):
        m = EP_ID_RE.match(s)
        if not m:
            raise ValueError(f"分集 id 非法: {ep!r}（形如 E01）")
        return f"E{int(m.group(1)):02d}"
    if s.isdigit():
        return f"E{int(s):02d}"
    raise ValueError(f"分集 id 非法: {ep!r}（形如 E01）")


def episode_dir(series_name: str, ep_id: str,
                 base_dir: Optional[str | Path] = None) -> Path:
    return episodes_dir(series_name, base_dir) / normalize_episode_id(ep_id)


def episode_drama_name(series_name: str, ep_id: str) -> str:
    """分集映射到老单剧流水线的剧名（稳定键，与集标题解耦）。

    形如 ``"<系列> E01"``：集改名不影响该键，老的
    ``/api/drama/{name}/state|start|retry``、分镜表 ``?name=``、SSE、
    runner 均可复用，无需新开渲染通道。
    """
    safe = sanitize_series_name(series_name)
    eid = normalize_episode_id(ep_id)
    return f"{safe} {eid}"


def characters_path(series_name: str,
                    base_dir: Optional[str | Path] = None) -> Path:
    return series_dir(series_name, base_dir) / "characters.json"


def scenes_path(series_name: str,
                base_dir: Optional[str | Path] = None) -> Path:
    return series_dir(series_name, base_dir) / "scenes.json"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def _ensure_characters_placeholder(sdir: Path) -> None:
    cpath = sdir / "characters.json"
    if not cpath.exists():
        _write_json(cpath, {"characters": [], "updated_at": _now_iso()})
    spath = sdir / "scenes.json"
    if not spath.exists():
        _write_json(spath, {"scenes": [], "updated_at": _now_iso()})


def _ensure_scenes_placeholder(sdir: Path) -> None:
    spath = sdir / "scenes.json"
    if not spath.exists():
        _write_json(spath, {"scenes": [], "updated_at": _now_iso()})


# ---------- 系列 CRUD ----------

def create_series(name: str, base_dir: Optional[str | Path] = None,
                  meta: Optional[dict] = None) -> dict:
    """新建系列；已存在 raise ValueError（幂等由 get/update 承担）。

    与旧单剧同目录混写防护（选 400 提示重名方案，不自动隔离）：
    若 ``outputs/<剧>/`` 已存在 ``script.json``/``state.json`` 但无
    ``series.json``，说明是 orchestrator 旧单剧产物，拒绝创建并提示换名
    （自动隔离到 episodes 子空间会造成两套真源混淆，故不采用）。
    """
    safe = sanitize_series_name(name)
    sdir = series_dir(safe, base_dir)
    spath = sdir / "series.json"
    if spath.exists():
        raise ValueError(f"系列 {safe!r} 已存在")
    if sdir.is_dir() and not spath.is_file():
        if (sdir / "script.json").is_file() or (sdir / "state.json").is_file():
            raise ValueError(
                f"剧名 {safe!r} 与旧单剧重名（outputs/{safe}/ 下已有 "
                f"script.json/state.json 但无 series.json），请换名或先备份迁移老产物")
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "episodes").mkdir(parents=True, exist_ok=True)
    _ensure_characters_placeholder(sdir)
    _ensure_scenes_placeholder(sdir)
    now = _now_iso()
    data: dict[str, Any] = {
        "name": safe,
        "title": safe,
        "total_seconds": 0.0,
        "episode_count": 0,
        "episodes": [],
        "created_at": now,
        "updated_at": now,
    }
    if isinstance(meta, dict):
        for k, v in meta.items():
            if k in ("name", "total_seconds", "episode_count", "episodes",
                     "created_at", "updated_at"):
                continue
            data[k] = v
        if isinstance(meta.get("title"), str) and meta["title"].strip():
            data["title"] = meta["title"].strip()
    _write_json(spath, data)
    return data


def list_series(base_dir: Optional[str | Path] = None) -> list[dict]:
    root = _outputs_root(base_dir)
    out: list[dict] = []
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        spath = child / "series.json"
        if not spath.is_file():
            continue
        try:
            out.append(get_series(child.name, base_dir))
        except (OSError, ValueError):
            continue
    return out


def _recalc_locked(sdir: Path, data: dict) -> dict:
    """按 episodes 索引重算 total_seconds/episode_count（不读分集文件）。"""
    eps = data.get("episodes", [])
    if not isinstance(eps, list):
        eps = []
    total = 0.0
    for e in eps:
        if isinstance(e, dict):
            try:
                total += float(e.get("total_seconds", 0) or 0)
            except (TypeError, ValueError):
                continue
    data["total_seconds"] = round(total, 3)
    data["episode_count"] = len([e for e in eps if isinstance(e, dict)])
    data["updated_at"] = _now_iso()
    return data


def get_series(name: str, base_dir: Optional[str | Path] = None) -> dict:
    sdir = series_dir(name, base_dir)
    spath = sdir / "series.json"
    if not spath.is_file():
        raise FileNotFoundError(f"系列 {name!r} 不存在（无 series.json）")
    data = _read_json(spath)
    if not isinstance(data, dict):
        raise ValueError(f"系列 {name!r} series.json 损坏")
    # 老索引回填 drama_name（内存态，缺键的分集按稳定规则补）
    # + style_override 缺键回填 ""（前端建集风格透传，旧数据默认空）
    try:
        _sname = str(data.get("name", name) or name)
        for _e in (data.get("episodes") or []):
            if isinstance(_e, dict) and not _e.get("drama_name") and _e.get("id"):
                try:
                    _e["drama_name"] = episode_drama_name(_sname, str(_e["id"]))
                except ValueError:
                    continue
        for _e in (data.get("episodes") or []):
            if isinstance(_e, dict) and "style_override" not in _e:
                _e["style_override"] = ""
    except Exception:
        pass
    data = _recalc_locked(sdir, data)
    # 回写聚合值（总量为各集之和，不静默丢分集索引）
    try:
        _write_json(spath, data)
    except OSError:
        pass
    return data


def update_series(name: str, patch: dict,
                  base_dir: Optional[str | Path] = None) -> dict:
    sdir = series_dir(name, base_dir)
    spath = sdir / "series.json"
    if not spath.is_file():
        raise FileNotFoundError(f"系列 {name!r} 不存在")
    data = _read_json(spath)
    if not isinstance(data, dict):
        raise ValueError(f"系列 {name!r} series.json 损坏")
    if not isinstance(patch, dict):
        raise ValueError("patch 须为 dict")
    for k in ("name", "total_seconds", "episode_count", "episodes",
              "created_at"):
        if k in patch:
            continue  # 聚合/只读字段不接受手写
    for k, v in patch.items():
        if k in ("name", "total_seconds", "episode_count", "episodes",
                 "created_at"):
            continue
        data[k] = v
    if isinstance(patch.get("title"), str) and patch["title"].strip():
        data["title"] = patch["title"].strip()
    data = _recalc_locked(sdir, data)
    _write_json(spath, data)
    return data


def delete_series(name: str, base_dir: Optional[str | Path] = None) -> bool:
    """删系列：只删 series.json + episodes/ + characters.json，老产物保留。

    不得 rmtree 整个剧目录（outputs/<剧>/ 可能混有 orchestrator 旧单剧的
    script.json/state.json，删库会误删老产物）。
    """
    sdir = series_dir(name, base_dir)
    spath = sdir / "series.json"
    if not spath.is_file():
        raise FileNotFoundError(f"系列 {name!r} 不存在（无 series.json）")
    try:
        spath.unlink()
    except FileNotFoundError:
        pass
    epath = sdir / "episodes"
    if epath.is_dir():
        shutil.rmtree(epath)
    cpath = sdir / "characters.json"
    try:
        if cpath.is_file():
            cpath.unlink()
    except OSError:
        pass
    scpath = sdir / "scenes.json"
    try:
        if scpath.is_file():
            scpath.unlink()
    except OSError:
        pass
    return True


def series_total_seconds(name: str,
                         base_dir: Optional[str | Path] = None) -> float:
    return float(get_series(name, base_dir).get("total_seconds", 0.0) or 0.0)


# ---------- 分集 CRUD ----------

def _next_episode_id(sdir: Path) -> str:
    edirs = sdir / "episodes"
    nums: list[int] = []
    if edirs.is_dir():
        for child in edirs.iterdir():
            m = EP_ID_RE.match(child.name.upper()) if child.is_dir() else None
            if m:
                try:
                    nums.append(int(m.group(1)))
                except ValueError:
                    continue
    return f"E{(max(nums) + 1) if nums else 1:02d}"


def _upsert_series_episode_index(sdir: Path, entry: dict) -> dict:
    spath = sdir / "series.json"
    if not spath.is_file():
        raise FileNotFoundError("系列不存在（无 series.json），请先建系列")
    data = _read_json(spath)
    if not isinstance(data, dict):
        raise ValueError("series.json 损坏")
    eps = data.get("episodes")
    if not isinstance(eps, list):
        eps = []
        data["episodes"] = eps
    for i, e in enumerate(eps):
        if isinstance(e, dict) and e.get("id") == entry.get("id"):
            eps[i] = entry
            break
    else:
        eps.append(entry)
    eps.sort(key=lambda e: str((e or {}).get("id", "")))
    data = _recalc_locked(sdir, data)
    _write_json(spath, data)
    return data


def _remove_series_episode_index(sdir: Path, ep_id: str) -> dict:
    spath = sdir / "series.json"
    data = _read_json(spath) if spath.is_file() else {}
    if not isinstance(data, dict):
        data = {}
    eps = data.get("episodes")
    if not isinstance(eps, list):
        eps = []
    data["episodes"] = [e for e in eps
                        if not (isinstance(e, dict) and e.get("id") == ep_id)]
    data = _recalc_locked(sdir, data)
    _write_json(spath, data)
    return data


def decode_upload_bytes(data: bytes) -> str:
    """上传字节解码：UTF-8 优先，失败回退 GBK；都失败 raise ValueError。"""
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("上传内容须为字节")
    try:
        return bytes(data).decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return bytes(data).decode("gbk")
    except UnicodeDecodeError as exc:
        raise ValueError("文件编码须为 UTF-8 或 GBK") from exc


def hash_source_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def prepare_source(source_text: str) -> dict:
    """原文预处理：记 source_chars/hash/truncated，超 12000 字截断前部。

    返回 {"stored": ..., "source_chars": int, "source_hash": str,
           "truncated": bool}。
    """
    original = source_text or ""
    chars = len(original)
    truncated = chars > SOURCE_MAX_CHARS
    stored = original[:SOURCE_MAX_CHARS] if truncated else original
    return {"stored": stored, "source_chars": chars,
            "source_hash": hash_source_text(original),
            "truncated": truncated}


def _normalize_style_override(v: Any) -> str:
    """风格覆盖归一：strip 后 40 字截断（前端建集透传，超长截断不断言）。"""
    return str(v or "").strip()[:40]


def create_episode(series_name: str, title: str = "",
                   source_text: str = "", total_seconds: Any = None,
                   base_dir: Optional[str | Path] = None,
                   source_filename: str = "",
                   episode_id: Optional[str] = None,
                   style_override: str = "") -> dict:
    safe = sanitize_series_name(series_name)
    sdir = series_dir(safe, base_dir)
    if not (sdir / "series.json").is_file():
        raise FileNotFoundError(f"系列 {series_name!r} 不存在，请先建系列")
    eid = normalize_episode_id(episode_id) if episode_id else _next_episode_id(sdir)
    edir = sdir / "episodes" / eid
    if edir.exists():
        raise ValueError(f"分集 {eid} 已存在")
    # total_seconds 可空：None/"" 表示未知、待 AI 规划定时（AI 自由发挥）；
    # 显式传入才校验 >0 并落盘，回写由 plan 完成后 update_episode 承担。
    total: float | None = None
    if total_seconds is None or (isinstance(total_seconds, str) and not total_seconds.strip()):
        total = None
    else:
        try:
            total = float(total_seconds)
        except (TypeError, ValueError):
            raise ValueError("total_seconds 须为数字") from None
        if total <= 0:
            raise ValueError("total_seconds 须为正数")
    prep = prepare_source(source_text or "")
    now = _now_iso()
    style_ov = _normalize_style_override(style_override)
    edir.mkdir(parents=True, exist_ok=True)
    (edir / "source.md").write_text(prep["stored"], encoding="utf-8")
    meta: dict[str, Any] = {
        "id": eid,
        "series": safe,
        "drama_name": episode_drama_name(safe, eid),
        "title": (title or "").strip() or eid,
        "total_seconds": round(total, 3) if total is not None else None,
        "style_override": style_ov,
        "source_chars": prep["source_chars"],
        "source_hash": prep["source_hash"],
        "truncated": prep["truncated"],
        "source_filename": source_filename or "",
        "created_at": now,
        "updated_at": now,
    }
    _write_json(edir / "meta.json", meta)
    _upsert_series_episode_index(sdir, {
        "id": eid, "title": meta["title"],
        "drama_name": meta["drama_name"],
        "total_seconds": meta["total_seconds"],
        "style_override": style_ov,
        "source_chars": meta["source_chars"],
        "source_hash": meta["source_hash"],
        "truncated": meta["truncated"],
        "updated_at": now,
    })
    return meta


def list_episodes(series_name: str,
                  base_dir: Optional[str | Path] = None) -> list[dict]:
    data = get_series(series_name, base_dir)
    eps = data.get("episodes", [])
    return [e for e in eps if isinstance(e, dict)]


def get_episode(series_name: str, ep_id: str,
                base_dir: Optional[str | Path] = None) -> dict:
    eid = normalize_episode_id(ep_id)
    edir = episode_dir(series_name, eid, base_dir)
    mpath = edir / "meta.json"
    if not mpath.is_file():
        # 回退读系列索引（老数据无 meta.json 时）
        for e in list_episodes(series_name, base_dir):
            if e.get("id") == eid:
                return dict(e)
        raise FileNotFoundError(f"分集 {ep_id!r} 不存在")
    meta = _read_json(mpath)
    if not isinstance(meta, dict):
        raise ValueError(f"分集 {ep_id!r} meta.json 损坏")
    # 老数据回填：缺 drama_name 的按稳定规则补（集改名不影响该键）
    if not meta.get("drama_name"):
        try:
            meta["drama_name"] = episode_drama_name(
                str(meta.get("series", series_name) or series_name), eid)
        except ValueError:
            pass
    # 老数据回填：缺 style_override 补 ""（读态，不写盘）
    if "style_override" not in meta:
        meta["style_override"] = ""
    return meta


def update_episode(series_name: str, ep_id: str, patch: dict,
                   base_dir: Optional[str | Path] = None) -> dict:
    eid = normalize_episode_id(ep_id)
    edir = episode_dir(series_name, ep_id, base_dir)
    mpath = edir / "meta.json"
    if not mpath.is_file():
        raise FileNotFoundError(f"分集 {ep_id!r} 不存在")
    meta = _read_json(mpath)
    if not isinstance(patch, dict):
        raise ValueError("patch 须为 dict")
    if isinstance(patch.get("title"), str) and patch["title"].strip():
        meta["title"] = patch["title"].strip()
    if "total_seconds" in patch and patch["total_seconds"] is not None:
        try:
            total = float(patch["total_seconds"])
        except (TypeError, ValueError):
            raise ValueError("total_seconds 须为数字") from None
        if total <= 0:
            raise ValueError("total_seconds 须为正数")
        meta["total_seconds"] = round(total, 3)
    if isinstance(patch.get("source_text"), str):
        prep = prepare_source(patch["source_text"])
        (edir / "source.md").write_text(prep["stored"], encoding="utf-8")
        meta["source_chars"] = prep["source_chars"]
        meta["source_hash"] = prep["source_hash"]
        meta["truncated"] = prep["truncated"]
    if "style_override" in patch and patch["style_override"] is not None:
        meta["style_override"] = _normalize_style_override(patch["style_override"])
    meta["updated_at"] = _now_iso()
    _write_json(mpath, meta)
    sdir = series_dir(series_name, base_dir)
    _upsert_series_episode_index(sdir, {
        "id": eid, "title": meta.get("title", eid),
        "drama_name": meta.get("drama_name") or episode_drama_name(
            str(meta.get("series", series_name) or series_name), eid),
        "total_seconds": meta.get("total_seconds", 0),
        "style_override": meta.get("style_override", ""),
        "source_chars": meta.get("source_chars", 0),
        "source_hash": meta.get("source_hash", ""),
        "truncated": bool(meta.get("truncated", False)),
        "updated_at": meta.get("updated_at", _now_iso()),
    })
    return meta


def delete_episode(series_name: str, ep_id: str,
                   base_dir: Optional[str | Path] = None) -> bool:
    eid = normalize_episode_id(ep_id)
    edir = episode_dir(series_name, eid, base_dir)
    if not edir.is_dir():
        raise FileNotFoundError(f"分集 {ep_id!r} 不存在")
    shutil.rmtree(edir)
    sdir = series_dir(series_name, base_dir)
    _remove_series_episode_index(sdir, eid)
    return True


# ---------- 分集文件读写 ----------

def read_episode_source(series_name: str, ep_id: str,
                        base_dir: Optional[str | Path] = None) -> str:
    p = episode_dir(series_name, ep_id, base_dir) / "source.md"
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def write_episode_plan(series_name: str, ep_id: str, plan: dict,
                       base_dir: Optional[str | Path] = None) -> Path:
    p = episode_dir(series_name, ep_id, base_dir) / "plan.json"
    _write_json(p, plan)
    return p


def read_episode_plan(series_name: str, ep_id: str,
                      base_dir: Optional[str | Path] = None) -> Optional[dict]:
    p = episode_dir(series_name, ep_id, base_dir) / "plan.json"
    if not p.is_file():
        return None
    try:
        obj = _read_json(p)
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def write_episode_script(series_name: str, ep_id: str, script: dict,
                         base_dir: Optional[str | Path] = None) -> Path:
    p = episode_dir(series_name, ep_id, base_dir) / "script.json"
    _write_json(p, script)
    return p


def prev_episode_summary(series_name: str, ep_id: str,
                         base_dir: Optional[str | Path] = None,
                         limit: int = 300) -> str:
    """前集梗概要（<=limit 字）：取前一集 source.md 首部，超长截断。"""
    eid = normalize_episode_id(ep_id)
    try:
        num = int(eid[1:])
    except ValueError:
        return ""
    if num <= 1:
        return ""
    prev_id = f"E{num - 1:02d}"
    text = read_episode_source(series_name, prev_id, base_dir).strip()
    if not text:
        return ""
    compact = " ".join(text.split())
    if len(compact) > limit:
        return compact[:limit]
    return compact
