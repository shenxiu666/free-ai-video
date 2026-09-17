"""opencode 二进制管理：内置优先 + 系统检测 + 自定义（docs/05 §5.8）。

opencode 仅为开发期 Agent（代码生成/调试），运行时不强依赖，缺失不影响出片。
本模块只做二进制定位与 `--version` 探测，不启动交互式 TUI，不读 Key 明文。

选择优先级：请求级 bin_path > ENV(OPENCODE_BIN/OPENCODE_MODE) > config/opencode.yaml。
mode 语义：builtin=只用内置；system=只用系统 PATH；custom=只用 bin_path；
auto=内置存在用内置，否则回退系统 PATH。默认 builtin。
"""

from __future__ import annotations

import json as _json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

try:
    import yaml  # type: ignore

    _YAML_OK = True
except Exception:
    yaml = None  # type: ignore
    _YAML_OK = False

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILTIN_EXE = REPO_ROOT / "tools" / "opencode" / "opencode.exe"
VERSION_FILE = REPO_ROOT / "tools" / "opencode" / "version.txt"
CONFIG_PATH = REPO_ROOT / "config" / "opencode.yaml"

MODES = ("builtin", "system", "custom", "auto")
DEFAULT_MODE = "builtin"

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)")


def pinned_version(path: str | Path | None = None) -> str:
    """锁定版本（tools/opencode/version.txt），缺失返回空串。"""
    try:
        p = Path(path) if path else VERSION_FILE
        # utf-8-sig：兼容 Windows 记事本/Set-Content 带 BOM 的写法
        return p.read_text(encoding="utf-8-sig").strip().split()[0]
    except (OSError, IndexError):
        return ""


def builtin_path() -> Path:
    """内置二进制路径（默认首选，不保证存在——缺失走 install.ps1 配给）。"""
    return BUILTIN_EXE


def parse_version_output(text: str) -> str:
    """从 `--version` 输出提取 x.y.z；提不到返回去空白原文（截断 64 字符）。"""
    m = _VERSION_RE.search(str(text or ""))
    if m:
        return m.group(1)
    return str(text or "").strip()[:64]


def _probe_argv(bin_path: str) -> list[str]:
    """Windows 安全 argv：npm 垫片（.ps1/.cmd/.bat）经解释器调用，exe 直接调用。

    一律 shell=False，避免路径空格/注入问题。
    """
    low = bin_path.lower()
    if low.endswith(".ps1"):
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                f"& '{bin_path}' --version"]
    if low.endswith(".cmd") or low.endswith(".bat"):
        comspec = os.environ.get("ComSpec", "cmd.exe")
        return [comspec, "/c", bin_path, "--version"]
    return [bin_path, "--version"]


def get_version(bin_path: str, timeout: float = 10.0) -> str:
    """运行 `<bin> --version` 并解析版本号；失败抛 RuntimeError（含原因）。"""
    if not bin_path or not str(bin_path).strip():
        raise RuntimeError("bin_path 为空")
    argv = _probe_argv(str(bin_path).strip())
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', errors='replace',
                              timeout=timeout, shell=False)
    except FileNotFoundError as exc:
        raise RuntimeError(f"找不到可执行文件：{bin_path}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"探测超时（{timeout}s）：{bin_path}") from exc
    except OSError as exc:
        raise RuntimeError(f"无法执行：{bin_path}（{exc}）") from exc
    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    if proc.returncode != 0 and not out:
        raise RuntimeError(f"退出码 {proc.returncode} 且无输出：{bin_path}")
    ver = parse_version_output(out)
    if not ver:
        raise RuntimeError(f"无法解析版本输出：{out[:120]}")
    return ver


def test_binary(bin_path: str, timeout: float = 15.0) -> dict:
    """供设置页“测试”按钮：返回 {ok, path, version|None, latency_ms, error|None}。"""
    start = time.perf_counter()
    try:
        ver = get_version(bin_path, timeout=timeout)
        return {"ok": True, "path": bin_path, "version": ver,
                "latency_ms": round((time.perf_counter() - start) * 1000, 1),
                "error": None}
    except RuntimeError as exc:
        return {"ok": False, "path": bin_path, "version": None,
                "latency_ms": round((time.perf_counter() - start) * 1000, 1),
                "error": str(exc)}


def _npm_global_exe() -> Optional[str]:
    """npm 全局真实 exe（绕过 .ps1/.cmd 垫片，便于直接调用）。"""
    appdata = os.environ.get("APPDATA", "")
    cands = []
    if appdata:
        cands.append(Path(appdata) / "npm" / "node_modules"
                     / "opencode-ai" / "bin" / "opencode.exe")
    for pf in (os.environ.get("ProgramFiles", ""),
               os.environ.get("ProgramFiles(x86)", "")):
        if pf:
            cands.append(Path(pf) / "nodejs" / "node_modules"
                         / "opencode-ai" / "bin" / "opencode.exe")
    for a in cands:
        if a.is_file():
            return str(a)
    return None


def _which_path() -> Optional[str]:
    """系统 PATH 中的 opencode（可能是 npm 垫片）；找不到返回 None。"""
    try:
        found = shutil.which("opencode")
    except Exception:
        found = None
    return found


def detect_candidates() -> list[dict]:
    """存在性检测（快，不跑 --version；版本探测走 probe_candidates）。

    每项 {source, path, exists}，source ∈ builtin/env/path/npm-global/user/custom。
    去重（同一路径只保留第一个来源），顺序即推荐优先级。
    """
    cands: list[dict] = []
    seen: set[str] = set()

    def _add(source: str, path: str | None) -> None:
        if not path:
            return
        p = str(path).strip()
        if not p or p.lower() in seen:
            return
        seen.add(p.lower())
        cands.append({"source": source, "path": p,
                      "exists": Path(p).is_file()})

    _add("builtin", str(BUILTIN_EXE))
    _add("env", os.environ.get("OPENCODE_BIN", "").strip() or None)
    _add("path", _which_path())
    _add("npm-global", _npm_global_exe())
    user_home = os.environ.get("USERPROFILE", "") or str(Path.home())
    _add("user", str(Path(user_home) / ".opencode" / "bin" / "opencode.exe"))
    sel = load_selection()
    if (sel.get("mode") == "custom" and sel.get("bin_path")):
        _add("custom", sel["bin_path"])
    return cands


def probe_candidates(timeout_per: float = 6.0) -> list[dict]:
    """全量版本探测（设置页“检测”按钮用；每项限时，慢项记 error 不阻塞）。"""
    out: list[dict] = []
    for c in detect_candidates():
        item = dict(c)
        item["version"] = None
        item["error"] = None
        if not c["exists"]:
            item["error"] = "文件不存在"
            out.append(item)
            continue
        try:
            item["version"] = get_version(c["path"], timeout=timeout_per)
        except RuntimeError as exc:
            item["error"] = str(exc)
        out.append(item)
    return out


# ---------- 选择（YAML + ENV） ----------

def load_selection(config_path: str | Path | None = None) -> dict:
    """读取选择：YAML < ENV(OPENCODE_MODE/OPENCODE_BIN)；恒返回 {mode, bin_path}。"""
    mode: str = DEFAULT_MODE
    bin_path: str = ""
    cfg = config_path or CONFIG_PATH
    if _YAML_OK:
        try:
            p = Path(cfg)
            if p.is_file():
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                if isinstance(data, dict):
                    if isinstance(data.get("mode"), str) and data["mode"].strip():
                        mode = data["mode"].strip().lower()
                    if isinstance(data.get("bin_path"), str):
                        bin_path = data["bin_path"].strip()
        except Exception:
            pass
    env_mode = os.environ.get("OPENCODE_MODE", "").strip().lower()
    if env_mode:
        mode = env_mode
    env_bin = os.environ.get("OPENCODE_BIN", "").strip()
    if env_bin:
        bin_path = env_bin
    if mode not in MODES:
        mode = DEFAULT_MODE
    return {"mode": mode, "bin_path": bin_path}


def save_selection(mode: str, bin_path: str = "",
                   config_path: str | Path | None = None) -> dict:
    """校验并持久化选择到 config/opencode.yaml；返回 load_selection() 结果。"""
    m = str(mode or "").strip().lower()
    if m not in MODES:
        raise ValueError(f"mode 非法：{mode!r}，允许 {MODES}")
    bp = str(bin_path or "").strip()
    if m == "custom":
        if not bp:
            raise ValueError("custom 模式须提供 bin_path（opencode 可执行文件绝对路径）")
        if not Path(bp).is_file():
            raise ValueError(f"bin_path 不存在：{bp}")
    cfg = Path(config_path) if config_path else CONFIG_PATH
    cfg.parent.mkdir(parents=True, exist_ok=True)
    header = ("# opencode 二进制选择（docs/05 §5.8）。无秘密值，可进 git。\n"
              "# 优先级：请求级 bin_path > ENV(OPENCODE_BIN/OPENCODE_MODE) > 本文件。\n"
              "# 设置页 POST /api/opencode/select 写入本文件（默认 builtin）。\n")
    if _YAML_OK:
        # 经 yaml 落盘：Windows 路径反斜杠由库负责转义（手拼双引号会把 \t \U 当转义）
        assert yaml is not None
        body = yaml.safe_dump({"mode": m, "bin_path": bp},
                              allow_unicode=True, sort_keys=False)
    else:
        esc = bp.replace("'", "''")
        body = f"mode: {m}\nbin_path: '{esc}'\n"
    cfg.write_text(header + body, encoding="utf-8")
    return load_selection(config_path)


def resolve_bin(mode: str = "", bin_path: str = "",
                env_bin: str = "", builtin: str = "",
                path_bin: str | None = None) -> dict:
    """纯函数：按优先级解出生效二进制 {path|None, source|None, reason}。

    优先级：显式 bin_path 参数 > ENV > mode。auto=内置存在则内置，否则系统 PATH。
    （调用方传入经 load_selection() 合并后的值；拆参便于单测。）
    """
    bp = str(bin_path or "").strip()
    if bp:
        return {"path": bp, "source": "custom",
                "reason": "显式 bin_path（请求级/ENV/custom 配置）"}
    eb = str(env_bin or "").strip()
    if eb:
        return {"path": eb, "source": "env",
                "reason": "ENV OPENCODE_BIN 覆盖"}
    m = str(mode or DEFAULT_MODE).strip().lower() or DEFAULT_MODE
    if m == "builtin":
        return {"path": builtin or str(BUILTIN_EXE), "source": "builtin",
                "reason": "设置页模式 builtin（默认）"}
    if m == "system":
        return {"path": path_bin, "source": "path",
                "reason": "设置页模式 system（系统 PATH）"}
    if m == "custom":
        return {"path": None, "source": None,
                "reason": "custom 模式但未配置 bin_path"}
    # auto
    if builtin and Path(builtin).is_file():
        return {"path": builtin, "source": "builtin",
                "reason": "auto：内置存在，优先内置"}
    if path_bin:
        return {"path": path_bin, "source": "path",
                "reason": "auto：内置缺失，回退系统 PATH"}
    return {"path": None, "source": None,
            "reason": "auto：内置与系统 PATH 均无可用二进制"}


def effective_binary(selection: dict | None = None) -> dict:
    """当前生效二进制：resolve + 存在性 + 版本（失败不抛，version=None）。"""
    sel = selection or load_selection()
    r = resolve_bin(sel.get("mode", ""), sel.get("bin_path", ""),
                    "", str(BUILTIN_EXE), _which_path())
    info: dict[str, Any] = {"path": r["path"], "source": r["source"],
                            "reason": r["reason"], "exists": False,
                            "version": None, "pinned": pinned_version(),
                            "pinned_match": None}
    if r["path"] and Path(r["path"]).is_file():
        info["exists"] = True
        try:
            info["version"] = get_version(r["path"])
        except RuntimeError:
            info["version"] = None
        if info["pinned"] and info["version"]:
            info["pinned_match"] = (info["version"] == info["pinned"])
    return info


def status() -> dict:
    """设置页 + /models 共用：{selection, effective, pinned_version, candidates}。"""
    sel = load_selection()
    return {"selection": sel, "effective": effective_binary(sel),
            "pinned_version": pinned_version(),
            "candidates": detect_candidates()}


# ---------- CLI 文本通道（免登录免费模型，`opencode run`） ----------

# 经 CLI 调用时拼到消息末尾的护栏：只要答案，不要工具动作
NO_TOOLS_GUARD = "（指令：直接回答，不要调用任何工具，不要读写文件，不要联网搜索。）"


def run_text(bin_path: str = "", model: str = "", prompt: str = "",
             system: str = "", timeout: float = 180.0) -> dict:
    """经 opencode CLI 跑文本（免登录免费通道）。

    `run --model provider/model --format json <message>`，拼接 text 事件正文。
    system 无独立角色，拼到消息最前。工作目录固定空 scratch 目录，工具
    即便被触发也碰不到仓库。返回 {"text", "cost"}；失败抛 RuntimeError。
    """
    target = (bin_path or "").strip()
    if not target:
        eff = effective_binary()
        target = eff.get("path") or ""
        if not target or not eff.get("exists"):
            raise RuntimeError(
                f"opencode 二进制不可用（{eff.get('reason', '')}）："
                "请到设置页选择或跑 tools/opencode/install.ps1 配给内置")
    full_model = (model or "").strip()
    if not full_model:
        raise RuntimeError("model 为空")
    if "/" not in full_model:
        full_model = f"opencode/{full_model}"
    message = (f"{system}\n\n{prompt}" if (system or "").strip() else prompt)
    message = f"{message}\n\n{NO_TOOLS_GUARD}"
    tmp_base = (os.environ.get("TEMP", "") or os.environ.get("TMP", "")).strip()
    scratch = Path(tmp_base) / "free-ai-video-opencode-run" if tmp_base else None
    if scratch is None:
        scratch = REPO_ROOT
    else:
        try:
            scratch.mkdir(parents=True, exist_ok=True)
        except OSError:
            scratch = REPO_ROOT
    argv = [target, "run", "--model", full_model, "--format", "json", message]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', errors='replace',
                              timeout=timeout, shell=False, cwd=str(scratch))
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"opencode CLI 超时（{timeout}s）：可重试或换模型") from exc
    except OSError as exc:
        raise RuntimeError(f"无法执行：{target}（{exc}）") from exc
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 and not out:
        err = (proc.stderr or "").strip().splitlines()
        tail = " ".join(err[-3:])[:300] if err else ""
        raise RuntimeError(f"opencode CLI 退出码 {proc.returncode}：{tail or '无输出'}")
    texts: list[str] = []
    cost: dict | None = None
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = _json.loads(line)
        except Exception:
            continue
        if not isinstance(ev, dict):
            continue
        part = ev.get("part", {}) if isinstance(ev.get("part"), dict) else {}
        if ev.get("type") == "text" and part.get("type") == "text":
            t = part.get("text", "")
            if isinstance(t, str) and t:
                texts.append(t)
        if ev.get("type") == "step_finish" and isinstance(part, dict):
            tokens = part.get("tokens")
            if isinstance(tokens, dict):
                cost = {"total": tokens.get("total"), "input": tokens.get("input"),
                        "output": tokens.get("output")}
    text = "".join(texts).strip()
    if not text:
        raise RuntimeError(f"opencode CLI 无文本输出：{out[:200] or '空'}")
    return {"text": text, "cost": cost}


# ---------- 免费模型拉取（`opencode models`，docs/05 §5.8） ----------

# 二进制缺失/执行失败时的内置候选（cost 全 0 的免费模型，见实测 `models --verbose`）
CURATED_FREE_MODELS: list[dict] = [
    {"provider": "opencode", "model": "big-pickle", "name": "Big Pickle",
     "free": True, "url": "https://opencode.ai/zen/v1"},
    {"provider": "opencode", "model": "mimo-v2.5-free", "name": "MiMo V2.5 Free",
     "free": True, "url": "https://opencode.ai/zen/v1"},
    {"provider": "opencode", "model": "nemotron-3-ultra-free",
     "name": "Nemotron 3 Ultra Free", "free": True,
     "url": "https://opencode.ai/zen/v1"},
    {"provider": "agnes", "model": "agnes-3.0-flash",
     "name": "agnes-3.0-flash（文本，可选）", "free": True,
     "url": "https://apihub.agnes-ai.com/v1"},
    {"provider": "agnes", "model": "agnes-2.5-flash",
     "name": "agnes-2.5-flash（文本备选）", "free": True,
     "url": "https://apihub.agnes-ai.com/v1"},
]

_REF_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*)/([A-Za-z0-9_./-]+)$")


def parse_models_plain(text: str) -> list[dict]:
    """解析 `opencode models` 纯文本输出（每行 `provider/model`）。"""
    out: list[dict] = []
    for line in str(text or "").splitlines():
        s = line.strip()
        m = _REF_RE.match(s)
        if m:
            out.append({"provider": m.group(1), "model": m.group(2),
                        "ref": s, "name": "", "free": None})
    return out


def parse_models_verbose(text: str) -> list[dict]:
    """解析 `opencode models --verbose`（`provider/model` 行 + JSON 元数据块）。

    free 判定：cost.input==0 且 cost.output==0（实测免费模型均为此）。
    """
    out: list[dict] = []
    cur_ref: str | None = None
    buf: list[str] = []

    def _flush() -> None:
        if cur_ref is None:
            return
        meta = None
        if buf:
            try:
                meta = _json.loads("\n".join(buf))
            except Exception:
                meta = None
        out.append(_verbose_item(cur_ref, meta))

    for line in str(text or "").splitlines():
        s = line.strip()
        m = _REF_RE.match(s)
        if m and not line.startswith((" ", "\t")):
            _flush()
            cur_ref = s
            buf = []
        elif cur_ref is not None:
            buf.append(line)
    _flush()
    return out


def _verbose_item(ref: str, meta: dict | None) -> dict:
    provider, _, model = ref.partition("/")
    item: dict[str, Any] = {"provider": provider, "model": model,
                            "ref": ref, "name": "", "free": None,
                            "url": "", "context": None, "output": None}
    if not isinstance(meta, dict):
        return item
    item["name"] = str(meta.get("name", "") or "")
    try:
        cost = meta.get("cost", {}) or {}
        item["free"] = (float(cost.get("input", 0)) == 0
                        and float(cost.get("output", 0)) == 0)
    except (TypeError, ValueError):
        item["free"] = None
    api = meta.get("api", {}) if isinstance(meta.get("api"), dict) else {}
    item["url"] = str(api.get("url", "") or "")
    limit = meta.get("limit", {}) if isinstance(meta.get("limit"), dict) else {}
    item["context"] = limit.get("context")
    item["output"] = limit.get("output")
    return item


def _run_models(bin_path: str, provider: str = "", verbose: bool = False,
                refresh: bool = False, timeout: float = 30.0) -> str:
    """跑 `opencode models [provider] [--verbose] [--refresh]`，返回 stdout。

    shell=False；非 0 退出且无输出才抛（告警类 stderr 不致命）。
    """
    argv = [bin_path, "models"]
    if provider:
        argv.append(provider)
    if verbose:
        argv.append("--verbose")
    if refresh:
        argv.append("--refresh")
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', errors='replace',
                              timeout=timeout, shell=False)
    except FileNotFoundError as exc:
        raise RuntimeError(f"找不到可执行文件：{bin_path}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"拉取超时（{timeout}s）：可重试或加 provider 过滤") from exc
    except OSError as exc:
        raise RuntimeError(f"无法执行：{bin_path}（{exc}）") from exc
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 and not out:
        err = (proc.stderr or "").strip()[:200]
        raise RuntimeError(f"models 退出码 {proc.returncode}：{err or '无输出'}")
    return out


def list_models(bin_path: str = "", provider: str = "opencode",
                refresh: bool = False,
                timeout: float = 30.0) -> dict:
    """拉取可用模型：live（当前生效二进制）失败则回内置候选，保证设置页可用。

    返回 {source: live|curated, bin, models: [...], note}。
    """
    target = (bin_path or "").strip()
    if not target:
        eff = effective_binary()
        target = eff.get("path") or ""
        if not target or not eff.get("exists"):
            return {"source": "curated", "bin": target or None,
                    "models": [dict(m, ref=f"{m['provider']}/{m['model']}")
                               for m in CURATED_FREE_MODELS],
                    "note": f"无可用二进制（{eff.get('reason', '')}），返回内置候选"}
    try:
        verbose_out = _run_models(target, provider, verbose=True,
                                  refresh=refresh, timeout=timeout)
        models = parse_models_verbose(verbose_out)
        if not models:
            plain = _run_models(target, provider, verbose=False,
                                refresh=False, timeout=timeout)
            models = parse_models_plain(plain)
        return {"source": "live", "bin": target, "models": models,
                "note": "" if models else "二进制返回空列表"}
    except RuntimeError as exc:
        return {"source": "curated", "bin": target,
                "models": [dict(m, ref=f"{m['provider']}/{m['model']}")
                           for m in CURATED_FREE_MODELS],
                "note": f"live 拉取失败（{exc}），返回内置候选"}
