"""FastAPI 入口：localhost:8000（docs/01.3）。

路由：
- GET  /health
- GET  /models（合并本地 config + opencode 双源占位，须同时列出 agnes/zen）
- POST /api/drama/new
- GET  /api/drama/{name}/state
- POST /api/drama/{name}/retry
- GET  /api/queue/stream（SSE：state + 日志）
- GET  /api/keys（掩码列表；无 KeyPool 则空列表 + backend_status）
- POST /api/keys（入库）
- GET  /api/text/status（文本通道提供商/模型/密钥来源）
- POST /api/text/custom-key（自建端点专用密钥，进加密池）
- POST /api/text/test（连通性测试）
- POST /api/drama/breakdown（AI 分镜初稿）
- GET  /api/opencode/status（opencode 二进制选择+生效项+候选列表）
- GET  /api/opencode/models（拉取可用模型，live 失败回内置候选）
- POST /api/opencode/detect（全量版本探测）
- POST /api/opencode/select（保存 mode/bin_path 到 config/opencode.yaml）
- POST /api/opencode/test（`<bin> --version` 可用性验证）

KeyPool/Config 由 D 模块提供（keypool.KeyPoolManager、config_loader.load_models_config）；
缺失时 try/except 降级为内存假实现，保证可启动。
CORS 仅 localhost。SSE 用 sse-starlette。
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

try:  # 允许 `uvicorn main`（workdir=backend）与 `import backend.main` 两种方式
    from orchestrator import (  # type: ignore
        create_drama as _create_drama,
        get_state as _get_state,
        retry_clip as _retry_clip,
        validate_script as _validate_script,
    )
    from orchestrator import _drama_dir as _drama_dir_fn  # type: ignore
except ImportError:  # pragma: no cover
    from backend.orchestrator import (  # type: ignore
        create_drama as _create_drama,
        get_state as _get_state,
        retry_clip as _retry_clip,
        validate_script as _validate_script,
    )
    from backend.orchestrator import _drama_dir as _drama_dir_fn  # type: ignore

try:
    from sse_starlette.sse import EventSourceResponse  # type: ignore
except ImportError:  # pragma: no cover — 降级：仍以 SSE 内容类型流式输出
    from fastapi.responses import StreamingResponse as EventSourceResponse  # type: ignore

# ---- KeyPool（D 提供；缺失降级内存） ----
_KEYPOOL_AVAILABLE = False
_KeyPoolManager: Any = None
_CUSTOM_TAG = "自建端点"  # 与 keypool.CUSTOM_ENDPOINT_TAG 同值（取不到模块时回退字面量）
try:
    from keypool import KeyPoolManager as _KPM, CUSTOM_ENDPOINT_TAG as _CET  # type: ignore
    _KeyPoolManager = _KPM
    _CUSTOM_TAG = _CET
    _KEYPOOL_AVAILABLE = True
except ImportError:
    try:
        from backend.keypool import KeyPoolManager as _KPM2, CUSTOM_ENDPOINT_TAG as _CET2  # type: ignore
        _KeyPoolManager = _KPM2
        _CUSTOM_TAG = _CET2
        _KEYPOOL_AVAILABLE = True
    except ImportError:
        _KEYPOOL_AVAILABLE = False

# ---- Config（D 提供；缺失用内置默认，见 docs/05.3） ----
_load_models_config: Any = None
try:
    from config_loader import load_models_config as _lm  # type: ignore
    _load_models_config = _lm
except ImportError:
    try:
        from backend.config_loader import load_models_config as _lm2  # type: ignore
        _load_models_config = _lm2
    except ImportError:
        _load_models_config = None

# ---- 文本 Provider（用户自配；AI 分镜与连通性测试用） ----
_TextNotConfigured: Any = None
_generate_text: Any = None
try:
    from providers.text import (  # type: ignore
        TextNotConfigured as _TNC,
        generate_text as _gt,
    )
    _TextNotConfigured = _TNC
    _generate_text = _gt
except ImportError:
    try:
        from backend.providers.text import (  # type: ignore
            TextNotConfigured as _TNC2,
            generate_text as _gt2,
        )
        _TextNotConfigured = _TNC2
        _generate_text = _gt2
    except ImportError:
        _TextNotConfigured = None
        _generate_text = None

# ---- AI 分镜（小说/剧本 → script.json 初稿） ----
_breakdown: Any = None
try:
    import breakdown as _bd  # type: ignore
    _breakdown = _bd
except ImportError:
    try:
        import backend.breakdown as _bd2  # type: ignore
        _breakdown = _bd2
    except ImportError:
        _breakdown = None

# ---- 加密基座（docs/03；缺失则密钥池退化为纯内存，/api/keys/activate 报 503） ----
_crypto: Any = None
try:
    import crypto as _crypto_mod  # type: ignore
    _crypto = _crypto_mod
except ImportError:
    try:
        import backend.crypto as _crypto_mod2  # type: ignore
        _crypto = _crypto_mod2
    except ImportError:
        _crypto = None

# 本机 .env（装配因子：派生密钥引用/salt；KEK/明文永不进文件）
REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = REPO_ROOT / ".env"
if _crypto is not None:
    try:
        _crypto.load_local_env(_ENV_PATH)
    except Exception:
        pass

# ---- 渲染执行引擎（docs/04；缺失则 start/retry 只改状态不干活） ----
_runner: Any = None
try:
    import runner as _runner_mod  # type: ignore
    _runner = _runner_mod
except ImportError:
    try:
        import backend.runner as _runner_mod2  # type: ignore
        _runner = _runner_mod2
    except ImportError:
        _runner = None

# ---- opencode 二进制管理（docs/05 §5.8；缺失则 /api/opencode/* 报 503） ----
_opencode_mgr: Any = None
try:
    import opencode_manager as _om  # type: ignore
    _opencode_mgr = _om
except ImportError:
    try:
        import backend.opencode_manager as _om2  # type: ignore
        _opencode_mgr = _om2
    except ImportError:
        _opencode_mgr = None


def _default_models() -> dict:
    return {
        "text": {"provider": "", "model": "", "fallback": {"mode": "manual"}},
        "image": {"provider": "agnes", "model": "agnes-image-2.5-flash",
                  "size": "1K", "ratio": "9:16"},
        "video": {"provider": "agnes", "model": "agnes-video-2.5-flash",
                  "size": "720P", "mode": "reference", "seconds": "8",
                  "aspect_ratio": "9:16"},
        "tts": {"provider": "edge-tts", "voice": "zh-CN-XiaoxiaoNeural"},
    }


def mask_key(key: str) -> str:
    """掩码：sk-前2~~~~后3；与 keypool.mask_key 统一（非sk-前缀也补sk-）。"""
    s = (key or "").strip()
    if not s:
        return ""
    core = s[3:] if s.startswith("sk-") else s
    head = (core[:2] if len(core) >= 2 else core).ljust(2, "~")
    tail = s[-3:] if len(s) >= 3 else s
    return f"sk-{head}~~~~{tail}"


# 内存假实现（KeyPool 缺失时）：{masked: {"masked", "kind"}}
_MEM_KEYS: dict[str, dict] = {}

# 全局单例池（进程内复用，否则每次 new 即丢数据）
_POOL_SINGLETON: Any = None
# 持久化标记：True=加密 DB 写透（重启可恢复）；False=纯内存（重启丢失）
_POOL_PERSISTENT: bool = False
# True=密封损坏/换机，需重新激活（写入口应锁定，读不受影响）
_POOL_NEEDS_REACTIVATION: bool = False


def _build_pool() -> Any | None:
    """构造池：密封+派生密钥齐全即走加密 DB（重启可恢复），否则纯内存。"""
    global _POOL_PERSISTENT, _POOL_NEEDS_REACTIVATION
    _POOL_PERSISTENT = False
    _POOL_NEEDS_REACTIVATION = False
    if not _KEYPOOL_AVAILABLE or _KeyPoolManager is None:
        return None
    mem = _KeyPoolManager()
    if _crypto is None:
        return mem
    try:
        seal_status = _crypto.get_backend_status()
    except Exception:
        return mem
    if seal_status == getattr(_crypto, "STATUS_NONE", "未激活"):
        return mem  # 从未激活：纯内存，UI 引导一键激活
    try:
        kek = _crypto.derive_kek()
    except Exception as exc:
        if type(exc).__name__ == "NeedReactivationError":
            _POOL_NEEDS_REACTIVATION = True
        return mem
    try:
        dbp = _crypto.get_db_path()
        pmgr = _KeyPoolManager.load(str(dbp), kek)
        _POOL_PERSISTENT = True
        return pmgr
    except Exception:
        return mem
    finally:
        try:
            del kek  # type: ignore[possibly-undefined]
        except Exception:
            pass


def _pool() -> Any | None:
    global _POOL_SINGLETON
    if not _KEYPOOL_AVAILABLE or _KeyPoolManager is None:
        return None
    if _POOL_SINGLETON is None:
        _POOL_SINGLETON = _build_pool()
    return _POOL_SINGLETON


def _migrate_mem_pool(old: Any, new: Any) -> int:
    """激活时把内存池明文条目迁入持久池（同仓白盒读 _keys；用量归零）。"""
    if old is None or new is None or old is new:
        return 0
    try:
        entries = list((getattr(old, "_keys", {}) or {}).values())
    except Exception:
        return 0
    migrated = 0
    for e in entries:
        raw = getattr(e, "raw_key", "") or ""
        if not raw:
            continue
        try:
            new.register_key(raw,
                             account_tag=getattr(e, "account_tag", "") or "",
                             pool_type=getattr(e, "pool_type", "free") or "free",
                             key_id=getattr(e, "id", None))
            migrated += 1
        except Exception:
            continue
        finally:
            raw = ""
    return migrated


def _backend_status() -> str:
    """TPM锁定/DPAPI兜底/未加密-仅调试/未激活：优先接 crypto.get_backend_status()。"""
    try:
        from crypto import get_backend_status as _gbs  # type: ignore
        return str(_gbs())
    except ImportError:
        pass
    try:
        from backend.crypto import get_backend_status as _gbs2  # type: ignore
        return str(_gbs2())
    except Exception:
        pass
    return "keypool" if _KEYPOOL_AVAILABLE else "memory-fallback"


def _list_keys_fallback() -> list[dict]:
    mgr = _pool()
    if mgr is not None:
        try:
            # 真实池：to_frontend_dict() 只吐掩码字段
            if hasattr(mgr, "to_frontend_dict"):
                items = mgr.to_frontend_dict()
                out: list[dict] = []
                for it in items or []:
                    if not isinstance(it, dict):
                        continue
                    out.append({
                        "id": it.get("mask", ""),
                        "mask": it.get("mask", ""),
                        "status": it.get("status", 2),
                        "display": it.get("display", ""),
                        "revoked": bool(it.get("revoked", False)),
                        "pool_type": it.get("pool_type", "free"),
                        "usage": it.get("usage", {}),
                        "limits": it.get("limits", {}),
                        "cooldown_until": None,
                        "cooldown_left_s": it.get("cooldown_left_s", 0),
                        "cooling": bool(it.get("cooldown_left_s", 0) and it.get("cooldown_left_s", 0) > 0),
                        "account_tag": it.get("account_tag", ""),
                        "shared_group": it.get("shared_group"),
                    })
                return out
            for attr in ("list_masked", "list_keys", "list"):
                if hasattr(mgr, attr):
                    items = getattr(mgr, attr)()
                    out = []
                    for it in items or []:
                        if isinstance(it, dict):
                            m = it.get("masked") or (
                                mask_key(str(it.get("key", "")))
                                if it.get("key") else "****"
                            )
                            out.append({"masked": m,
                                        "kind": it.get("kind", "unknown")})
                        else:
                            out.append({"masked": mask_key(str(it)),
                                        "kind": "unknown"})
                    return out
        except Exception:
            pass
    return list(_MEM_KEYS.values())


def _add_key_fallback(key: str, kind: str = "agnes", pool_type: str = "free", account_tag: str = "") -> dict:
    mgr = _pool()
    if mgr is not None:
        try:
            if hasattr(mgr, "register_key"):
                entry = mgr.register_key(key, account_tag=account_tag or "", pool_type=pool_type or "free")
                m = getattr(entry, "mask", mask_key(key))
                return {"masked": m, "mask": m, "kind": kind or "agnes"}
            for attr in ("add_key", "add", "store"):
                if hasattr(mgr, attr):
                    getattr(mgr, attr)(key)
                    break
        except Exception:
            pass
    masked = mask_key(key)
    _MEM_KEYS[masked] = {"masked": masked, "mask": masked, "kind": kind or "agnes"}
    return _MEM_KEYS[masked]


def _local_models() -> dict:
    if _load_models_config is not None:
        try:
            cfg = _load_models_config()
            if isinstance(cfg, dict) and cfg:
                return cfg
        except Exception:
            pass
    return _default_models()


def _text_cfg() -> dict:
    """文本配置（settings 页 text 节）；缺失回默认值。"""
    local = _local_models()
    text = local.get("text", {}) if isinstance(local, dict) else {}
    return text if isinstance(text, dict) else {}


def _acquire_text_key(purpose: str = "agnes") -> Any:
    """从密钥池取文本 Key（entry.raw_key 即明文，仅内存短暂驻留）。

    归属隔离（docs/05 §5.9）：purpose=agnes 排除“自建端点”专用密钥；
    purpose=custom 只取该备注（无匹配直接 400，不回退，避免串用）。
    池不可用/无可用 Key 时抛 HTTPException（400，附带去设置页的指引）。
    """
    mgr = _pool()
    if mgr is None:
        raise HTTPException(status_code=400, detail="密钥池不可用：请先在「密钥池+设置」页录入 Key")
    try:
        if purpose == "custom":
            return mgr.acquire("text", require_tag=_CUSTOM_TAG)
        return mgr.acquire("text", exclude_tag=_CUSTOM_TAG)
    except Exception as exc:
        if type(exc).__name__ == "PoolExhausted":
            retry = getattr(exc, "retry_after", None)
            hint = f"（约 {retry:.0f}s 后重试）" if isinstance(retry, (int, float)) else ""
            if purpose == "custom":
                raise HTTPException(status_code=400, detail="自建端点未配置专用密钥：请在设置页文本节填写端点密钥（进加密池，不存配置文件）") from exc
            raise HTTPException(status_code=400, detail=f"文本 Key 暂无可用{hint}：请检查密钥池状态或稍后重试") from exc
        raise HTTPException(status_code=500, detail=f"取 Key 失败：{exc}") from exc


def _release_text_key(entry: Any, ok: bool, status: int = 0, body: str = "",
                      latency_ms: float = 0.0) -> None:
    """归还文本 Key（失败信息进 A/B/C 分类；自身异常不污染主流程）。"""
    try:
        mgr = _pool()
        if mgr is None:
            return
        mgr.release(entry, ok=ok,
                    err=None if ok else {"status": status, "body": body},
                    cost=1, kind="text", latency_ms=latency_ms)
    except Exception:
        pass


def _is_zen_provider(provider: str) -> bool:
    p = (provider or "").strip().lower()
    return p in ("zen", "opencode-zen", "opencode/zen")


def _run_text_with_config(cfg: dict, prompt: str, system: str = "",
                           max_tokens_cap: int | None = None) -> tuple[dict, float]:
    """用文本配置跑一次模型，返回 (result, latency_ms)。

    opencode-zen 走本地 CLI（免登录，不碰密钥池）；其余走密钥池取用归还。
    provider/model 留空 400；模块缺失 503；Provider 异常转可操作 HTTP。
    """
    import time as _time

    provider = str(cfg.get("provider", "") or "")
    model = str(cfg.get("model", "") or "")
    if not provider or not model:
        raise HTTPException(status_code=400, detail="文本模型未配置：请先去「密钥池+设置」页填写提供商/模型")
    if _generate_text is None:
        raise HTTPException(status_code=503, detail="文本通道不可用（providers.text 缺失）")
    timeout = float(cfg.get("timeout_s", 120) or 120)
    temperature = float(cfg.get("temperature", 0.7))
    max_tokens = int(cfg.get("max_tokens", 8192))
    if max_tokens_cap is not None:
        max_tokens = min(max_tokens, max_tokens_cap)
    start_ts = _time.perf_counter()
    if _is_zen_provider(provider):
        try:
            result = _generate_text(
                prompt, provider="opencode-zen", model=model,
                fallback_mode="manual", timeout=timeout,
                temperature=temperature, max_tokens=max_tokens, system=system)
        except Exception as exc:
            if _TextNotConfigured is not None and isinstance(exc, _TextNotConfigured):
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if isinstance(exc, ValueError):
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            raise _provider_error_to_http(exc) from exc
        return result, round((_time.perf_counter() - start_ts) * 1000, 1)
    purpose = "custom" if _normalize_text_provider(provider) == "openai-compatible" else "agnes"
    entry = _acquire_text_key(purpose)
    api_key = getattr(entry, "raw_key", "") or ""
    try:
        result = _generate_text(
            prompt, provider=provider, model=model,
            fallback_mode="manual", api_key=api_key,
            base_url=str(cfg.get("base_url", "") or ""),
            timeout=timeout, temperature=temperature,
            max_tokens=max_tokens, system=system)
    except Exception as exc:
        _release_text_key(entry, False,
                           status=getattr(getattr(exc, "response", None), "status_code", 0),
                           body=str(exc)[:300])
        if _TextNotConfigured is not None and isinstance(exc, _TextNotConfigured):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise _provider_error_to_http(exc) from exc
    latency_ms = round((_time.perf_counter() - start_ts) * 1000, 1)
    _release_text_key(entry, True, latency_ms=latency_ms)
    return result, latency_ms


def _normalize_text_provider(provider: str) -> str:
    p = (provider or "").strip().lower()
    if p in ("zen", "opencode-zen", "opencode/zen"):
        return "opencode-zen"
    if p.startswith("http://") or p.startswith("https://"):
        return "openai-compatible"
    return p


def _provider_error_to_http(exc: Exception) -> HTTPException:
    """文本 Provider 异常 → HTTP（401/403/429 给出可操作提示，其余 502；明文绝不外泄）。"""
    try:
        import httpx as _httpx  # type: ignore
    except ImportError:
        return HTTPException(status_code=502, detail=f"文本调用失败：{exc}")
    if isinstance(exc, _httpx.HTTPStatusError):
        resp = exc.response
        code = resp.status_code if resp is not None else 0
        snippet = ""
        try:
            snippet = (resp.text or "")[:200] if resp is not None else ""
        except Exception:
            snippet = ""
        if code in (401, 403):
            return HTTPException(status_code=400, detail=f"文本鉴权失败（{code}）：请检查密钥池 Key 是否有效/是否作废。{snippet}")
        if code == 429:
            return HTTPException(status_code=400, detail=f"文本限流（429）：已按规则冷却该 Key，请稍后重试。{snippet}")
        return HTTPException(status_code=502, detail=f"文本服务异常（{code}）：{snippet or exc}")
    if isinstance(exc, _httpx.TimeoutException):
        return HTTPException(status_code=502, detail=f"文本请求超时：{exc}，可到设置页调大超时")
    return HTTPException(status_code=502, detail=f"文本调用失败：{exc}")


def _opencode_sources() -> dict:
    """解析仓库 opencode.json，双源占位：须同时列出 agnes + zen。"""
    root = Path(__file__).resolve().parent.parent
    info: dict[str, Any] = {"providers": ["agnes", "opencode-zen"],
                            "models": [], "config_present": False}
    for cand in (root / "opencode.json", Path.cwd() / "opencode.json"):
        if cand.exists():
            try:
                data = json.loads(cand.read_text(encoding="utf-8"))
                provs = list((data.get("provider") or {}).keys()) or ["agnes"]
                if "zen" not in provs and "opencode-zen" not in provs:
                    provs = provs + ["opencode-zen"]  # zen 经 auth 登录，不在 JSON 写 key
                info = {"providers": provs,
                        "models": sorted({m for p in (data.get("provider") or {}).values()
                                          for m in ((p.get("models") or {}).keys() if isinstance(p, dict) else [])}),
                        "config_present": True}
            except (OSError, ValueError):
                pass
            break
    # 本地 opencode 二进制（docs/05 §5.8）：失败不抛，cli=None  printed as null
    try:
        if _opencode_mgr is not None:
            st = _opencode_mgr.status()
            eff = st.get("effective", {})
            info["cli"] = {"path": eff.get("path"), "source": eff.get("source"),
                           "exists": eff.get("exists", False),
                           "version": eff.get("version"),
                           "pinned": st.get("pinned_version"),
                           "pinned_match": eff.get("pinned_match"),
                           "mode": (st.get("selection") or {}).get("mode")}
    except Exception:
        info["cli"] = {"path": None, "source": None, "exists": False,
                       "version": None, "error": "status 探测失败"}
    return info


def _require_opencode_mgr() -> Any:
    if _opencode_mgr is None:
        raise HTTPException(status_code=503, detail="opencode 管理器不可用（backend/opencode_manager.py 缺失）")
    return _opencode_mgr


app = FastAPI(title="free-ai-video backend", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_methods=["*"],
    allow_headers=["*"],
)


class DramaNewRequest(BaseModel):
    name: str | None = None
    script: dict | None = None
    # 前端 B 表单直发兼容：{title,total_seconds,aspect,clip_seconds,style,brief}
    title: str | None = None
    total_seconds: float | None = None
    aspect: str | None = None
    clip_seconds: str | int | float | None = None
    style: str | None = None
    brief: str | None = None


class ScriptSaveRequest(BaseModel):
    script: dict | None = None
    clips: list | None = None
    # 允许裸 script dict 直发（前端 saveDramaScript 直接 JSON.stringify(script)）
    model_config = {"extra": "allow"}


class RetryRequest(BaseModel):
    clip_id: str = Field(min_length=1)
    stages: Optional[list[str]] = None


class RenderStartRequest(BaseModel):
    clips: Optional[list[str]] = None


class KeyAddRequest(BaseModel):
    key: str | None = None
    kind: str = "agnes"
    # 前端 C 兼容：{raw_key,pool_type,account_tag}
    raw_key: str | None = None
    pool_type: str | None = None
    account_tag: str | None = None
    model_config = {"extra": "allow"}


class OpencodeSelectRequest(BaseModel):
    mode: str = Field(min_length=1)
    bin_path: str = ""


class OpencodeTestRequest(BaseModel):
    bin_path: str | None = None


class BreakdownRequest(BaseModel):
    name: str = Field(min_length=1)
    total_seconds: float = 60
    aspect: str = "9:16"
    clip_seconds: str | int | float = "8"
    style: str = ""
    source_text: str = Field(min_length=1)


class TextTestRequest(BaseModel):
    # 可选覆盖（设置页“先测后存”）；留空用已保存的文本配置
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    timeout_s: float | None = None


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "free-ai-video-backend"}


@app.get("/models")
def models() -> dict:
    """合并本地 config + opencode 双源占位。"""
    return {
        "local": _local_models(),
        "opencode": _opencode_sources(),
    }


@app.post("/api/drama/new")
def drama_new(req: DramaNewRequest) -> dict:
    # 兼容两种入参：{name,script}（规范）与 {title,total_seconds,...}（前端表单）
    name = (req.name or req.title or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="剧名不能为空（name/title 必填其一）")
    script: dict
    if isinstance(req.script, dict) and req.script:
        script = req.script
    elif req.total_seconds is not None:
        total = float(req.total_seconds)
        clip_sec = str(req.clip_seconds if req.clip_seconds is not None else "8")
        n = max(1, int(round(total / float(clip_sec))) if float(clip_sec) > 0 else 1)
        per = total / n
        aspect = req.aspect or "9:16"
        script = {
            "title": name,
            "total_seconds": total,
            "aspect": aspect,
            "resolution": "720x1280" if aspect == "9:16" else "1280x720",
            "clip_seconds": clip_sec,
            "character_refs": [],
            "clips": [
                {
                    "id": f"s{i+1:02d}",
                    "start": round(i * per, 3),
                    "duration": round(per if i < n - 1 else total - i * per, 3),
                    "narration": (req.brief or "") if i == 0 else "",
                    "image_prompt": f"[主体]{name}[场景]{req.style or ''}[风格]{req.style or ''}[光照]自然光[构图]{aspect}[质量]1K,高细节",
                    "video_prompt": f"[主体]{name}[动作]缓慢推进[场景]{req.style or ''}[运镜]缓慢推镜[光照]自然光[风格]{req.style or ''}",
                    "video_mode": "text",
                }
                for i in range(n)
            ],
        }
    else:
        raise HTTPException(status_code=400, detail="缺少 script（{name,script} 或 {title,total_seconds,...} 二选一）")
    try:
        _validate_script(script)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state = _create_drama(name, script)
    return {"ok": True, "state": state, "script": script}


def _repair_mux_done_without_artifact(name: str, st: dict) -> bool:
    """老任务谎话收敛：mux==done 但单镜成品缺失/为空 → 降为 failed。

    只处理读到的这一份 state；无改动不写盘；任何异常内部吞掉，绝不搞崩正常读状态。
    新代码写 done 前必验成品，故正常状态永远触发不了本分支。
    """
    try:
        clips = (st or {}).get("clips", {})
        if not isinstance(st, dict) or not isinstance(clips, dict):
            return False
        changed = False
        for cid, stages in clips.items():
            if not isinstance(stages, dict) or stages.get("mux") != "done":
                continue
            try:
                p = _drama_dir_fn(name) / "final_clips" / f"{cid}.mp4"
                ok = p.is_file() and p.stat().st_size > 0
            except (OSError, ValueError):
                ok = False
            if not ok:
                stages["mux"] = "failed"
                changed = True
        if changed:
            st["updated_at"] = datetime.now(timezone.utc).isoformat()
            sp = _drama_dir_fn(name) / "state.json"
            sp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        return changed
    except Exception:
        return False


@app.get("/api/drama/{name}/state")
def drama_state(name: str) -> dict:
    try:
        st = _get_state(name)
        _repair_mux_done_without_artifact(name, st)
        # 附带 script.json（分镜表加载用；缺失则 script=None，前端不得用空架子覆盖本地草稿）
        script: dict | None = None
        try:
            script_path = _drama_dir_fn(name) / "script.json"
            if script_path.is_file():
                loaded = json.loads(script_path.read_text(encoding="utf-8"))
                script = loaded if isinstance(loaded, dict) else None
        except (OSError, ValueError):
            script = None
        # 兼容前端两种形状：{ok,state} + 顶层展平 clips 适配 queue store
        finals = st.get("finals", []) if isinstance(st, dict) else []
        try:
            final_exists = (_drama_dir_fn(name) / "final.mp4").is_file()
        except Exception:
            final_exists = False
        mux_detail = st.get("mux_detail") if isinstance(st, dict) else None
        return {"ok": True, "state": st, "script": script,
                "name": name, "clips": _state_to_clip_list(st),
                "finals": finals if isinstance(finals, list) else [],
                "final_exists": final_exists, "mux_detail": mux_detail}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _state_to_clip_list(st: dict) -> list[dict]:
    """orchestrator state {clips:{id:{image..}}} -> 前端 [{id,image,video,tts,mux}]。

    透出 message/progress（如 state 有）：stage 值为 dict（{status,message,progress}）
    时拆出状态+附带字段；另兼容 `{stage}_message` / `{stage}_progress` 平铺写法。
    """
    clips = (st or {}).get("clips", {})
    out: list[dict] = []

    def _stage_val(stages: dict, stage: str) -> tuple[Any, Any, Any]:
        v = stages.get(stage, "pending")
        if isinstance(v, dict):
            return (v.get("status", "pending"), v.get("message"), v.get("progress"))
        return (v, stages.get(f"{stage}_message"), stages.get(f"{stage}_progress"))

    if isinstance(clips, dict):
        for cid, stages in clips.items():
            if not isinstance(stages, dict):
                continue
            item: dict[str, Any] = {"id": str(cid)}
            for stage in ("image", "video", "tts", "mux"):
                status, message, progress = _stage_val(stages, stage)
                item[stage] = status
                if message is not None:
                    item[f"{stage}_message"] = message
                if progress is not None:
                    item[f"{stage}_progress"] = progress
            if isinstance(stages.get("message"), str):
                item["message"] = stages["message"]
            if stages.get("progress") is not None:
                item["progress"] = stages["progress"]
            out.append(item)
    elif isinstance(clips, list):
        for c in clips:
            if isinstance(c, dict) and c.get("id"):
                item = {"id": str(c.get("id")), "image": c.get("image", "pending"),
                        "video": c.get("video", "pending"), "tts": c.get("tts", "pending"),
                        "mux": c.get("mux", "pending")}
                if isinstance(c.get("message"), str):
                    item["message"] = c["message"]
                if c.get("progress") is not None:
                    item["progress"] = c["progress"]
                out.append(item)
    return out


@app.post("/api/drama/{name}/script")
def drama_save_script(name: str, req: dict) -> dict:
    """保存 script.json（分镜表编辑）：接受 {script:{...}} 或裸 script dict。"""
    script: dict | None = None
    if isinstance(req, dict):
        if isinstance(req.get("script"), dict):
            script = req["script"]
        elif "clips" in req and "total_seconds" in req:
            script = req
        elif "clips" in req:
            # 尝试与现存 script 合并（经 _drama_dir_fn 净化剧名，防路径穿越）
            try:
                cur_path = _drama_dir_fn(name) / "script.json"
                cur = json.loads(cur_path.read_text(encoding="utf-8")) if cur_path.exists() else {}
            except Exception:
                cur = {}
            script = {**cur, **req}
    if not isinstance(script, dict) or not script:
        raise HTTPException(status_code=400, detail="缺少 script（{script:{...}} 或裸 script）")
    try:
        _validate_script(script)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        state = _create_drama(name, script)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "state": state, "script": script}


@app.post("/api/drama/{name}/retry")
def drama_retry(name: str, req: RetryRequest) -> dict:
    try:
        state = _retry_clip(name, req.clip_id, stages=req.stages)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 联动真开跑：只跑该镜（幂等：已在跑则只复位状态）
    started, note = _kick_render(name, [req.clip_id])
    return {"ok": True, "state": state, "render_started": started,
            "render_note": note}


def _render_ctx() -> dict:
    """组装 runner 上下文（pool + 图/视频/TTS 配置）。"""
    local = _local_models()
    return {"pool": _pool(),
            "image": local.get("image", {}) if isinstance(local, dict) else {},
            "video": local.get("video", {}) if isinstance(local, dict) else {},
            "tts": local.get("tts", {}) if isinstance(local, dict) else {}}


def _kick_render(name: str, clips: Optional[list[str]] = None) -> tuple[bool, str]:
    """尽力启动渲染 worker，返回 (started, note)。缺条件只给提示，不抛。"""
    if _runner is None:
        return False, "渲染引擎缺失（backend/runner.py），只复位了状态"
    try:
        _get_state(name)
    except FileNotFoundError:
        return False, f"后端无该剧 {name!r}"
    except Exception as exc:
        return False, f"读状态失败：{exc}"
    if _pool() is None:
        return False, "密钥池不可用：请先激活并录入 Key，再点开始渲染"
    if _runner.is_running(name):
        return False, "渲染已在跑（重复点击不会重复开工）"
    try:
        ok = _runner.start_render(name, _render_ctx(), only_clips=clips)
    except Exception as exc:
        return False, f"启动失败：{exc}"
    if not ok:
        return False, "渲染已在跑（重复点击不会重复开工）"
    scope = f"（仅 {clips[0]}）" if clips and len(clips) == 1 else ""
    return True, f"已开跑{scope}，进度看SSE日志与分镜状态"


@app.post("/api/drama/{name}/start")
def drama_start(name: str, req: RenderStartRequest) -> JSONResponse:
    """开始渲染（全剧或指定分镜）：起后台 worker，真跑图→视频→配音→合成。"""
    started, note = _kick_render(name, list(req.clips or []))
    if not started and "已在跑" in note:
        raise HTTPException(status_code=409, detail=note)
    if not started:
        raise HTTPException(status_code=400, detail=note)
    return JSONResponse({"ok": True, "note": note})


@app.get("/api/drama/{name}/render-status")
def drama_render_status(name: str) -> dict:
    """渲染是否在跑（队列页运行指示用）。"""
    running = bool(_runner is not None and _runner.is_running(name))
    return {"ok": True, "name": name, "running": running}


async def _queue_events(name: Optional[str] = None):
    """SSE：state + 日志心跳（2s）。state 含 clips 列表版便于前端直染。"""
    while True:
        payload: dict[str, Any] = {"log": []}
        try:
            if name:
                st = _get_state(name)
                _repair_mux_done_without_artifact(name, st)
                payload["state"] = st
                payload["name"] = name
                payload["clips"] = _state_to_clip_list(st)
                log_path = (Path(__file__).resolve().parent.parent
                            / "outputs" / name / "render.log")
                if log_path.exists():
                    try:
                        lines = log_path.read_text(encoding="utf-8").splitlines()
                        payload["log"] = lines[-50:]
                    except OSError:
                        pass
            else:
                payload["state"] = None
        except FileNotFoundError as exc:
            payload = {"state": None, "log": [], "note": str(exc)}
        yield {"event": "state", "data": json.dumps(payload, ensure_ascii=False)}
        await asyncio.sleep(2)


@app.get("/api/queue/stream")
async def queue_stream(name: Optional[str] = None):
    gen = _queue_events(name)
    try:
        # sse-starlette 风格（dict event/data）
        return EventSourceResponse(gen)  # type: ignore[arg-type]
    except TypeError:  # 降级 StreamingResponse：手动拼 SSE 帧
        async def _raw():
            async for ev in gen:
                yield f"event: {ev['event']}\ndata: {ev['data']}\n\n"
        return EventSourceResponse(_raw(), media_type="text/event-stream")  # type: ignore


@app.get("/api/keys")
def keys_list() -> dict:
    return {
        "keys": _list_keys_fallback(),
        "backend_status": _backend_status(),
    }


@app.post("/api/keys")
def keys_add(req: KeyAddRequest) -> JSONResponse:
    raw = (req.key or req.raw_key or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="key 不能为空（key/raw_key 二选一）")
    pool_type = (req.pool_type or "free").strip() or "free"
    if pool_type not in ("free", "enterprise", "tokenplan"):
        # kind 兼容：agnes -> free
        pool_type = "free"
    account_tag = (req.account_tag or "").strip()
    kind = (req.kind or "agnes").strip() or "agnes"
    # KEK/API 明文绝不进日志：仅回掩码
    item = _add_key_fallback(raw, kind, pool_type, account_tag)
    # 主动清零局部明文引用
    raw = ""
    return JSONResponse({"ok": True, "masked": item.get("masked", item.get("mask", "")),
                         "mask": item.get("mask", item.get("masked", "")),
                         "backend_status": _backend_status()})


@app.get("/api/models")
def api_models() -> dict:
    """前端 C 兼容别名：与 GET /models 的 local 部分同形。"""
    return _local_models()


@app.post("/api/models")
def api_models_save(payload: dict) -> dict:
    """保存模型参数到 config/models.yaml（白名单字段+合法性校验）。"""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload 须为 dict")
    allowed_img = {"1K", "2K", "3K", "4K"}
    allowed_ratio = {"1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9"}
    img = payload.get("image", {}) if isinstance(payload.get("image"), dict) else {}
    if img.get("size") and img["size"] not in allowed_img:
        raise HTTPException(status_code=400, detail=f"image.size 非法：{img['size']!r}")
    if img.get("ratio") and img["ratio"] not in allowed_ratio:
        raise HTTPException(status_code=400, detail=f"image.ratio 非法：{img['ratio']!r}")
    vid = payload.get("video", {}) if isinstance(payload.get("video"), dict) else {}
    if vid.get("size") and vid["size"] != "720P":
        raise HTTPException(status_code=400, detail='video.size 只允许字符串 "720P"')
    if vid.get("mode") and vid["mode"] not in ("text", "keyframe", "reference"):
        raise HTTPException(status_code=400, detail=f"video.mode 非法：{vid['mode']!r}")
    if vid.get("seconds") and str(vid["seconds"]) not in {str(i) for i in range(4, 13)}:
        raise HTTPException(status_code=400, detail=f"video.seconds 非法：{vid['seconds']!r}")
    root = Path(__file__).resolve().parent.parent
    cfg_path = root / "config" / "models.yaml"
    try:
        import re as _re

        import yaml  # type: ignore
        current: dict = {}
        if cfg_path.exists():
            current = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        # 浅合并白名单顶层
        for k in ("text", "image", "video", "tts"):
            if isinstance(payload.get(k), dict):
                base = current.get(k, {}) if isinstance(current.get(k), dict) else {}
                current[k] = {**base, **payload[k]}
        # 强制字符串类型：size/seconds/ratio/aspect 必须保持字符串（Agnes 拒数字）
        for section, keys in (("image", ("size", "ratio")), ("video", ("size", "seconds", "mode", "aspect_ratio"))):
            sec = current.get(section)
            if isinstance(sec, dict):
                for kk in keys:
                    if kk in sec and sec[kk] is not None and not isinstance(sec[kk], str):
                        sec[kk] = str(sec[kk])
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        dumped = yaml.safe_dump(current, allow_unicode=True, sort_keys=False)
        # 防 YAML 1.1 六十进制：9:16 等含冒号标量必须带引号，否则下次 load 变成 556
        def _quote(m: "_re.Match[str]") -> str:
            key, val = m.group(1), m.group(2).strip()
            if not val or val[0] in "\"'":
                return m.group(0)
            return f"{key}\"{val}\""

        dumped = _re.sub(
            r"(?m)^(\s*(?:ratio|aspect_ratio|size|seconds|clip_seconds):\s*)([^\s\"\'#\n]+)",
            _quote,
            dumped,
        )
        cfg_path.write_text(dumped, encoding="utf-8")
        return {"ok": True, "saved": True}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"保存失败：{exc}") from exc


@app.get("/api/opencode/status")
def opencode_status() -> dict:
    """opencode 二进制状态：选择+生效项+锁定版本+候选列表（docs/05 §5.8）。"""
    mgr = _require_opencode_mgr()
    try:
        return {"ok": True, **mgr.status()}
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/opencode/detect")
def opencode_detect() -> JSONResponse:
    """全量版本探测（设置页“检测”按钮；每项限时，慢项记 error）。"""
    mgr = _require_opencode_mgr()
    try:
        return JSONResponse({"ok": True, "candidates": mgr.probe_candidates()})
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/opencode/select")
def opencode_select(req: OpencodeSelectRequest) -> JSONResponse:
    """保存二进制选择（mode + custom bin_path），写入 config/opencode.yaml。"""
    mgr = _require_opencode_mgr()
    try:
        selection = mgr.save_selection(req.mode, req.bin_path or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"写入配置失败：{exc}") from exc
    try:
        st = mgr.status()
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "selection": selection,
                         "effective": st.get("effective")})


@app.post("/api/drama/breakdown")
def drama_breakdown(req: BreakdownRequest) -> dict:
    """AI 分镜：小说/剧本原文 → script.json 初稿（不落盘，前端展示后用户可改再保存）。

    文本通道按配置自动选择：opencode-zen 走本地 CLI（免登录），其余走密钥池。
    固定 prompt 产严格 JSON，服务端归一化后经 validate_script 校验才返回。
    """
    if _breakdown is None:
        raise HTTPException(status_code=503, detail="AI 分镜不可用（breakdown 模块缺失）")
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="剧名不能为空")
    try:
        total = float(req.total_seconds)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="总时长须为数字") from None
    if total < 60:
        raise HTTPException(status_code=400, detail=f"总时长下限 60s，当前 {total}s")
    per = str(req.clip_seconds)
    if per not in {str(i) for i in range(4, 13)}:
        raise HTTPException(status_code=400, detail=f"单镜时长须为 4-12（字符串），当前 {req.clip_seconds!r}")
    aspect = (req.aspect or "9:16").strip()
    if aspect not in ("9:16", "16:9"):
        raise HTTPException(status_code=400, detail=f"画幅非法：{aspect!r}（仅 9:16 / 16:9）")
    source = (req.source_text or "").strip()
    if not source:
        raise HTTPException(status_code=400, detail="请粘贴小说/剧本原文后再分解")
    cfg = _text_cfg()
    system, user = _breakdown.build_breakdown_prompt(
        name, total, aspect, per, (req.style or "").strip(), source)
    result, latency_ms = _run_text_with_config(cfg, user, system=system)
    try:
        raw = _breakdown.extract_json(result.get("text", ""))
        script = _breakdown.coerce_script(raw, name, total, aspect, per,
                                          (req.style or "").strip())
        script["generated_by"] = result.get("generated_by", "")
        _validate_script(script)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"AI 分镜结果未通过校验：{exc}，可重试或改短原文") from exc
    return {"ok": True, "script": script,
            "generated_by": result.get("generated_by", ""),
            "latency_ms": latency_ms,
            "source_chars": len(source),
            "truncated": len(source) > _breakdown.SOURCE_MAX_CHARS}


@app.post("/api/text/test")
def text_test(req: TextTestRequest) -> JSONResponse:
    """连通性测试：用已保存配置（可被请求字段覆盖）真实 ping 一次文本通道。

    发一条极短 prompt，返回延迟与模型原样回复（截断 200 字）；HTTPS 通道
    经密钥池取用归还（明文不出内存），opencode-zen 走本地 CLI（免密钥池）。
    """
    cfg = _text_cfg()
    if req.provider is not None:
        cfg["provider"] = req.provider
    if req.model is not None:
        cfg["model"] = req.model
    if req.base_url is not None:
        cfg["base_url"] = req.base_url
    if req.temperature is not None:
        cfg["temperature"] = req.temperature
    if req.max_tokens is not None:
        cfg["max_tokens"] = req.max_tokens
    if req.timeout_s is not None:
        cfg["timeout_s"] = req.timeout_s
    result, latency_ms = _run_text_with_config(
        cfg, "连通性测试：请只回复“OK”二字，不要输出其他内容。",
        max_tokens_cap=512)
    provider = str(cfg.get("provider", "") or "")
    model = str(cfg.get("model", "") or "")
    reply = str(result.get("text", "") or "").strip()[:200]
    return JSONResponse({"ok": True, "provider": provider, "model": model,
                         "latency_ms": latency_ms, "reply": reply,
                         "generated_by": result.get("generated_by", "")})


class TextCustomKeyRequest(BaseModel):
    raw_key: str = Field(min_length=1)


class ActivateRequest(BaseModel):
    force: bool = False


@app.get("/api/text/status")
def text_status() -> dict:
    """文本通道状态：当前提供商/模型 + 密钥来源（池-Agnes / CLI-免登录 / 自建专用）。"""
    cfg = _text_cfg()
    provider = str(cfg.get("provider", "") or "")
    model = str(cfg.get("model", "") or "")
    norm = _normalize_text_provider(provider)
    if norm == "opencode-zen":
        key_source = "本地 CLI（免登录）"
        custom = {"configured": False, "mask": None}
    elif norm == "openai-compatible":
        key_source = "自建端点专用密钥"
        mgr = _pool()
        try:
            custom = mgr.custom_endpoint_key_status() if mgr is not None else {"configured": False, "mask": None}
        except Exception:
            custom = {"configured": False, "mask": None}
    else:
        key_source = "密钥池（仅 Agnes Key）"
        custom = {"configured": False, "mask": None}
    return {"ok": True, "provider": provider, "model": model,
            "key_source": key_source, "custom_key": custom}


@app.post("/api/text/custom-key")
def text_custom_key(req: TextCustomKeyRequest) -> JSONResponse:
    """录入自建端点专用密钥：进加密密钥池（备注“自建端点”），不存配置文件。

    明文只进本次请求 body，返回掩码后内存引用即清零。
    """
    raw = (req.raw_key or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="密钥不能为空")
    mgr = _pool()
    if mgr is None or not hasattr(mgr, "register_key"):
        raw = ""
        raise HTTPException(status_code=400, detail="密钥池不可用：无法保存专用密钥")
    try:
        entry = mgr.register_key(raw, account_tag=_CUSTOM_TAG, pool_type="free")
        mask = getattr(entry, "mask", mask_key(raw))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        raw = ""
    return JSONResponse({"ok": True, "mask": mask})


@app.get("/api/keys/persist-status")
def keys_persist_status() -> dict:
    """持久化状态：是否激活/写透/需重新激活 + DB 落盘情况 + 内存条目数。"""
    mgr = _pool()
    mem_count = 0
    try:
        if mgr is not None and hasattr(mgr, "to_frontend_dict"):
            mem_count = len(getattr(mgr, "_keys", {}) or {})
        else:
            mem_count = len(_MEM_KEYS)
    except Exception:
        mem_count = 0
    db_path: str | None = None
    db_exists = False
    if _crypto is not None:
        try:
            db_path = str(_crypto.get_db_path())
            db_exists = Path(db_path).is_file()
        except Exception:
            pass
    status = _backend_status()
    active_marks = {"TPM锁定", "DPAPI兜底", "未加密-仅调试"}
    if _crypto is not None:
        try:
            active_marks = {_crypto.STATUS_TPM, _crypto.STATUS_DPAPI, _crypto.STATUS_PLAIN}
        except Exception:
            pass
    return {"ok": True, "persistent": bool(_POOL_PERSISTENT),
            "activated": status in active_marks,
            "backend_status": status,
            "needs_reactivation": bool(_POOL_NEEDS_REACTIVATION),
            "db_exists": db_exists, "db_path": db_path,
            "memory_keys": mem_count}


@app.post("/api/keys/activate")
def keys_activate(req: ActivateRequest) -> JSONResponse:
    """一键激活（本机自签发，docs/03 首次激活流程）：

    先试直接解锁（密封+派生密钥完好即恢复，不碰任何文件）；不行再重签：
    生成派生密钥→写本机 .env→密封机器码→旧库解不开则归档（.bak-时间戳，
    不静默覆盖）→建库→重建持久池→迁移内存 Key。
    响应绝不含任何秘密值。
    """
    global _POOL_SINGLETON, _POOL_PERSISTENT
    if _crypto is None or _KeyPoolManager is None:
        raise HTTPException(status_code=503, detail="加密基座缺失，无法激活")
    if _POOL_PERSISTENT and not req.force:
        st = keys_persist_status()
        st["already"] = True
        return JSONResponse(st)
    import base64 as _b64
    import secrets as _secrets
    import time as _time
    import uuid as _uuid

    old_mem = _POOL_SINGLETON
    # 0) 先试无损解锁：密封与派生密钥都完好就直接恢复
    _POOL_SINGLETON = None
    _pool()
    if _POOL_PERSISTENT and not req.force:
        st = keys_persist_status()
        st.update({"already": True, "migrated": _migrate_mem_pool(old_mem, _POOL_SINGLETON),
                   "archived": None, "seal": st.get("backend_status", "")})
        return JSONResponse(st)
    _POOL_SINGLETON = old_mem  # 解锁失败：恢复会话内存池（保用量计数），继续走重签
    # 1) 派生密钥：沿用 .env 现有（非 force），否则重签
    existing = (os.environ.get("FREEAI_DERIVED_KEY") or "").strip()
    if existing and not req.force:
        kid = (os.environ.get("FREEAI_DERIVED_KEY_ID") or "").strip() or _uuid.uuid4().hex[:12]
        save_derived = existing
    else:
        save_derived = _secrets.token_urlsafe(32)
        kid = _uuid.uuid4().hex[:12]
    salt = (os.environ.get("FREEAI_KEYPOOL_SALT") or "").strip()
    if not salt or req.force:
        salt = _b64.b64encode(_secrets.token_bytes(16)).decode("ascii")
    try:
        _crypto.save_local_env({"FREEAI_DERIVED_KEY": save_derived,
                                "FREEAI_DERIVED_KEY_ID": kid,
                                "FREEAI_KEYPOOL_SALT": salt}, _ENV_PATH)
        _crypto.load_local_env(_ENV_PATH)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"读写本机 .env 失败：{exc}") from exc
    finally:
        save_derived = ""
    # 2) 密封机器码（TPM 优先，无则 DPAPI 兜底；状态如实上报）
    try:
        seal = _crypto.seal_machine_code()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"密封机器码失败：{exc}") from exc
    # 3) 派生新 KEK，旧库解不开则归档
    try:
        new_kek = _crypto.derive_kek()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"KEK 派生失败（{type(exc).__name__}），请重试或 force 重签") from exc
    archived: str | None = None
    try:
        dbp = _crypto.get_db_path()
        recs = _crypto.load_key_records(dbp)
        if recs:
            try:
                _crypto.decrypt_key(recs[0].get("nonce", ""), recs[0].get("ct", ""), new_kek)
            except Exception:
                bak = dbp.with_name(f"{dbp.name}.bak-{int(_time.time())}")
                dbp.rename(bak)
                archived = str(bak)
        _crypto.init_db(dbp)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"DB 处理失败：{exc}") from exc
    finally:
        try:
            del new_kek
        except Exception:
            pass
    # 4) 重建持久池 + 迁移内存 Key（仍非持久则恢复会话池，不丢用量）
    _POOL_SINGLETON = None
    _pool()
    if _POOL_PERSISTENT and getattr(_POOL_SINGLETON, "_kek", None):
        migrated = _migrate_mem_pool(old_mem, _POOL_SINGLETON)
    else:
        _POOL_PERSISTENT = False
        migrated = 0
        if old_mem is not None:
            _POOL_SINGLETON = old_mem
        raise HTTPException(
            status_code=500,
            detail="持久池未生效（keypool 未接上加密基座）：请从仓库根用 "
                   "python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 启动")
    st = keys_persist_status()
    st.update({"already": False, "migrated": migrated, "archived": archived,
               "seal": seal.get("status", "") if isinstance(seal, dict) else ""})
    return JSONResponse(st)


@app.get("/api/opencode/models")
def opencode_models(provider: str = "opencode", refresh: bool = False) -> dict:
    """拉取可用模型（默认 opencode 免费通道）：live 失败自动回内置候选。

    provider 留空拉全量（含 agnes）；refresh=true 从 models.dev 刷新缓存（较慢）。
    每项 {provider, model, ref, name, free, url, context, output}。
    """
    mgr = _require_opencode_mgr()
    try:
        result = mgr.list_models(provider=(provider or "").strip(),
                                 refresh=bool(refresh))
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, **result}


@app.post("/api/opencode/test")
def opencode_test(req: OpencodeTestRequest) -> JSONResponse:
    """运行 `<bin> --version` 验证可用性（默认测当前生效项）。"""
    mgr = _require_opencode_mgr()
    target = (req.bin_path or "").strip()
    if not target:
        try:
            eff = mgr.effective_binary()
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        target = eff.get("path") or ""
        if not target:
            raise HTTPException(status_code=400,
                                detail=f"无可用二进制：{eff.get('reason', '')}，请先在设置页选择或配给内置（tools/opencode/install.ps1）")
    return JSONResponse({"ok": True, **mgr.test_binary(target)})


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
