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
from fastapi.responses import FileResponse, JSONResponse
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
    total_seconds: float | None = None
    aspect: str = "9:16"
    clip_seconds: str | int | float | None = None
    style: str = ""
    source_text: str = Field(min_length=1)
    # 可选：所属系列名（传了则加载该系列角色+场景库做资产注入与缺图检出）
    series: str | None = None


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
            "scene_refs": [],
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

    统一方案（与系列单集一致，AI 自由发挥）：
    - total/clip 均可空：双显式才均匀切分（旧行为兼容），否则先跑 Planner；
      total 缺失 → AI 定总时长+镜表，total 显式但 clip 缺失 → AI 定镜表。
    文本通道按配置自动选择：opencode-zen 走本地 CLI（免登录），其余走密钥池。
    固定 prompt 产严格 JSON，服务端归一化后经 validate_script 校验才返回。
    """
    if _breakdown is None:
        raise HTTPException(status_code=503, detail="AI 分镜不可用（breakdown 模块缺失）")
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="剧名不能为空")
    _raw_total: Any = req.total_seconds
    if isinstance(_raw_total, str) and not _raw_total.strip():
        _raw_total = None
    total: float | None = None
    if _raw_total is not None:
        try:
            total = float(_raw_total)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="总时长须为数字") from None
        if total <= 0:
            raise HTTPException(status_code=400, detail=f"总时长须为正数，当前 {total}s")
    _raw_clip: Any = req.clip_seconds
    is_explicit_clip = not (
        _raw_clip is None or (isinstance(_raw_clip, str) and not str(_raw_clip).strip())
        or str(_raw_clip).strip().lower() == "auto")
    per = "8"
    if is_explicit_clip:
        per = str(_raw_clip).strip()
        if per not in {str(i) for i in range(4, 13)}:
            raise HTTPException(status_code=400, detail=f"单镜时长须为 4-12（字符串），当前 {req.clip_seconds!r}")
    aspect = (req.aspect or "9:16").strip()
    if aspect not in ("9:16", "16:9"):
        raise HTTPException(status_code=400, detail=f"画幅非法：{aspect!r}（仅 9:16 / 16:9）")
    source = (req.source_text or "").strip()
    if not source:
        raise HTTPException(status_code=400, detail="请粘贴小说/剧本原文后再分解")
    style = (req.style or "").strip()
    durations: list[float] | None = None
    style_eff: str | None = None
    # 非双显式 → 先 Planner（与系列 auto-plan 同方案）
    if not (total is not None and is_explicit_clip):
        _planner = globals().get("_M1_planner")
        if _planner is None:
            raise HTTPException(status_code=503, detail="规划模块缺失（backend/planner.py）")
        cfg0 = _text_cfg()
        system0, user0 = _planner.build_planner_prompt(name, total, style, source)
        result0, _ = _run_text_with_config(cfg0, user0, system=system0)
        try:
            raw0 = _planner.parse_plan_json(result0.get("text", ""))
            plan0 = _planner.coerce_plan(raw0, total, style)
        except ValueError as exc:
            raise HTTPException(status_code=502, detail=f"规划结果未通过校验：{exc}") from exc
        try:
            total = float(plan0.get("total_seconds", 0) or 0)
        except (TypeError, ValueError):
            raise HTTPException(status_code=502, detail="自动规划 total 非法") from None
        _durs0 = plan0.get("clip_durations")
        if isinstance(_durs0, list) and _durs0:
            durations = list(_durs0)
        _se0 = str(plan0.get("style_effective", "") or "").strip()
        style_eff = _se0 or None
        if _se0:
            style = _se0
    assert total is not None and total > 0
    cfg = _text_cfg()
    _asset = _series_assets((req.series or "").strip())
    system, user = _breakdown.build_breakdown_prompt(
        name, total, aspect, per, style, source,
        durations=durations, style_effective=style_eff,
        characters_summary=_asset.get("chars_summary", ""),
        prev_summary="",
        cast_plan=None,
        scenes_summary=_asset.get("scenes_summary", ""),
        asset_lines=_asset.get("asset_lines", ""),
        scene_plan=None)
    result, latency_ms = _run_text_with_config(cfg, user, system=system)
    try:
        raw = _breakdown.extract_json(result.get("text", ""))
        script = _breakdown.coerce_script(raw, name, total, aspect, per,
                                          style,
                                          durations=durations,
                                          style_effective=style_eff)
        script["generated_by"] = result.get("generated_by", "")
        _fill_script_character_refs(script, _asset.get("lib_chars", []),
                                    _asset.get("lib_scenes", []))
        _validate_script(script)
        try:
            _find_missing = getattr(_breakdown, "find_missing_assets", None)
            missing = _find_missing(script, _asset.get("available", set())) \
                if callable(_find_missing) else []
        except Exception:
            missing = []
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"AI 分镜结果未通过校验：{exc}，可重试或改短原文") from exc
    return {"ok": True, "script": script,
            "generated_by": result.get("generated_by", ""),
            "latency_ms": latency_ms,
            "source_chars": len(source),
            "truncated": len(source) > _breakdown.SOURCE_MAX_CHARS,
            "missing_assets": missing if isinstance(missing, list) else []}


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


# ---- 系列/角色/立绘/规划（Builder M1；docs/01.6、docs/04.5/04.10） ----
# 仅追加，不改动现有 /api/drama/* 行为。
from fastapi import Request as _M1Request  # noqa: E402

_M1_series: Any = None
_M1_planner: Any = None
_M1_characters: Any = None
try:
    import series as _smod  # type: ignore
    _M1_series = _smod
except ImportError:
    try:
        import backend.series as _smod2  # type: ignore
        _M1_series = _smod2
    except ImportError:
        _M1_series = None
try:
    import planner as _pmod  # type: ignore
    _M1_planner = _pmod
except ImportError:
    try:
        import backend.planner as _pmod2  # type: ignore
        _M1_planner = _pmod2
    except ImportError:
        _M1_planner = None
try:
    import characters as _cmod  # type: ignore
    _M1_characters = _cmod
except ImportError:
    try:
        import backend.characters as _cmod2  # type: ignore
        _M1_characters = _cmod2
    except ImportError:
        _M1_characters = None

_M1_scenes: Any = None
try:
    import scenes as _scmod  # type: ignore
    _M1_scenes = _scmod
except ImportError:
    try:
        import backend.scenes as _scmod2  # type: ignore
        _M1_scenes = _scmod2
    except ImportError:
        _M1_scenes = None

_M1_generate_image: Any = None
try:
    from providers.agnes_image import generate_image as _gi  # type: ignore
    _M1_generate_image = _gi
except ImportError:
    try:
        from backend.providers.agnes_image import generate_image as _gi2  # type: ignore
        _M1_generate_image = _gi2
    except ImportError:
        _M1_generate_image = None

# 别名：测试可 monkeypatch main._generate_image 兼容旧命名
_generate_image = _M1_generate_image


def _require_series_mods() -> tuple[Any, Any, Any]:
    if _M1_series is None or _M1_planner is None or _M1_characters is None:
        raise HTTPException(status_code=503,
                            detail="系列模块缺失（backend/series.py/planner.py/characters.py）")
    return _M1_series, _M1_planner, _M1_characters


def _scenes_mod() -> Any:
    """场景库模块（可选依赖；缺失返回 None，调用方降级为空库）。"""
    return globals().get("_M1_scenes")


def _load_scene_lib(series_name: str) -> list:
    """best-effort 读系列场景库（失败返 []，不阻断分镜）。"""
    mod = _scenes_mod()
    if mod is None or not series_name:
        return []
    try:
        items = mod.load_scenes(series_name)
        return [c for c in (items or []) if isinstance(c, dict)]
    except Exception:
        return []


def _series_assets(series_name: str) -> dict:
    """系列资产上下文（角色+场景）：摘要 + 可抄写路径清单 + 存在集合。

    返回 {"lib_chars", "lib_scenes", "chars_summary", "scenes_summary",
           "asset_lines", "available"}；模块缺失/读失败逐项降级为空。
    """
    lib_chars: list = []
    lib_scenes: list = []
    chars_summary = ""
    scenes_summary = ""
    char_lines = ""
    scene_lines = ""
    if series_name:
        try:
            _series, _planner, _chars = _require_series_mods()
            try:
                lib_chars = _chars.load_characters(series_name)
            except Exception:
                lib_chars = []
            try:
                chars_summary = _chars.summarize_characters(
                    [c for c in (lib_chars or []) if isinstance(c, dict)])
            except Exception:
                chars_summary = ""
            # 角色资产行：有图列主图路径（复用 images[0]，与 select 规则一致）
            try:
                rows = []
                for c in (lib_chars or []):
                    if not isinstance(c, dict):
                        continue
                    nm = str(c.get("name", "") or "").strip()
                    if not nm:
                        continue
                    main = ""
                    imgs = c.get("images", [])
                    if isinstance(imgs, list):
                        for im in imgs:
                            s = str(im or "").strip()
                            if s:
                                main = s
                                break
                    rows.append(f"- {nm}：{main}" if main else f"- {nm}（无图）")
                char_lines = "\n".join(rows).strip()
            except Exception:
                char_lines = ""
        except HTTPException:
            pass
        lib_scenes = _load_scene_lib(series_name)
        mod = _scenes_mod()
        if mod is not None:
            try:
                scenes_summary = mod.summarize_scenes(lib_scenes)
            except Exception:
                scenes_summary = ""
            try:
                scene_lines = mod.scene_asset_lines(lib_scenes)
            except Exception:
                scene_lines = ""
    parts = []
    if char_lines:
        parts.append("角色立绘：\n" + char_lines)
    if scene_lines:
        parts.append("场景图：\n" + scene_lines)
    asset_lines = "\n".join(parts).strip()
    available: set[str] = set()
    for lib in (lib_chars, lib_scenes):
        for c in (lib or []):
            if not isinstance(c, dict):
                continue
            imgs = c.get("images", [])
            if isinstance(imgs, list):
                for im in imgs:
                    s = str(im or "").strip()
                    if s:
                        available.add(s)
    return {"lib_chars": lib_chars, "lib_scenes": lib_scenes,
            "chars_summary": chars_summary, "scenes_summary": scenes_summary,
            "asset_lines": asset_lines, "available": available}


class SeriesNewRequest(BaseModel):
    name: str = Field(min_length=1)
    title: str | None = None
    synopsis: str | None = None
    style: str | None = None
    model_config = {"extra": "allow"}


class SeriesCreateAliasRequest(BaseModel):
    """POST /api/series 别名入参：前端只发 {title,synopsis?,style?}。

    name 为空时回退 title，否则 400；其余与 /api/series/new 同逻辑。
    """
    name: str | None = None
    title: str | None = None
    synopsis: str | None = None
    style: str | None = None
    model_config = {"extra": "allow"}


class SeriesEpisodePatchRequest(BaseModel):
    title: str | None = None
    total_seconds: float | None = None
    source_text: str | None = None
    style_override: str | None = None
    model_config = {"extra": "allow"}


class SeriesPlanRequest(BaseModel):
    episode_id: str | None = None
    total_seconds: float | None = None
    style: str | None = None
    source_text: str | None = None
    characters_summary: str | None = None
    prev_summary: str | None = None
    model_config = {"extra": "allow"}


class SeriesBreakdownRequest(BaseModel):
    aspect: str | None = "9:16"
    clip_seconds: str | int | float | None = "8"
    style: str | None = ""
    source_text: str | None = None
    model_config = {"extra": "allow"}


class CharacterExtractRequest(BaseModel):
    source_text: str | None = None
    episode_id: str | None = None
    first_seen: str | None = None
    model_config = {"extra": "allow"}


class CharacterCreateRequest(BaseModel):
    name: str | None = None
    desc: str | None = None
    model_config = {"extra": "allow"}


class CharacterMergeRequest(BaseModel):
    source_ids: list[str] | None = None
    target_id: str | None = None
    model_config = {"extra": "allow"}


def _parse_style_override(v: Any) -> str:
    """三通道风格透传归一：strip 后 40 字截断（落盘前 series 层再截一次）。"""
    return str(v or "").strip()[:40]


def _do_create_episode(name: str, title: Any, total_seconds: Any,
                       source_text: str, source_filename: str = "",
                       style_override: Any = "") -> dict:
    """建集公共落盘 helper（JSON/multipart/query/upload 四入口复用）。

    校验语义：total 可空（None/"" 即未知、待 AI 定，不再默认 60）；
    显式传入才校验 >0；原文非空；style_override 透传 series 层。
    """
    _series, _planner, _chars = _require_series_mods()
    if total_seconds is None or (isinstance(total_seconds, str) and not str(total_seconds).strip()):
        total_f = None
    else:
        try:
            total_f = float(total_seconds)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="total_seconds 须为数字") from None
        if total_f <= 0:
            raise HTTPException(status_code=400, detail=f"总时长须为正数，当前 {total_f}s")
    if not (source_text or "").strip():
        raise HTTPException(status_code=400, detail="请提供原文（source_text 或上传 md/txt）")
    try:
        return _series.create_episode(
            name, title=str(title or "").strip() or "E",
            source_text=source_text, total_seconds=total_f,
            source_filename=source_filename or "",
            style_override=_parse_style_override(style_override))
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _parse_upload_form(form: Any) -> dict:
    """multipart 公共解析（episode-create 与 /episodes/upload 共用）。

    返回 {title,total_seconds,source_text,source_filename,style_override}；
    缺 python-multipart 由调用方按现有 400 指引抛（本函数只做字段提取）。
    """
    _series, _planner, _chars = _require_series_mods()
    title = str(form.get("title", "") or "")
    ts = form.get("total_seconds", None)
    try:
        total_seconds = None if ts in (None, "") else float(ts)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="total_seconds 须为数字") from None
    style_override = _parse_style_override(form.get("style_override", ""))
    upload = form.get("file")
    if upload is None:
        for k in ("source", "upload", "md", "txt"):
            if form.get(k) is not None and hasattr(form.get(k), "read"):
                upload = form.get(k)
                break
    source_text = ""
    source_filename = ""
    if upload is not None and hasattr(upload, "read"):
        filename = str(getattr(upload, "filename", "") or "")
        source_filename = filename
        if filename and not filename.lower().endswith((".md", ".txt")):
            raise HTTPException(status_code=400, detail=f"仅支持 md/txt，当前 {filename!r}")
        try:
            data = await upload.read()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"读取上传失败：{exc}") from exc
        if len(data or b"") > 2 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="文件超 2MB 上限")
        try:
            source_text = _series.decode_upload_bytes(data or b"")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not title and filename:
            title = filename.rsplit(".", 1)[0]
    else:
        source_text = str(form.get("source_text", "") or "")
        source_filename = str(form.get("source_filename", "") or "")
        if not title:
            title = str(form.get("title", "") or "")
    return {"title": title, "total_seconds": total_seconds,
            "source_text": source_text, "source_filename": source_filename,
            "style_override": style_override}


def _episode_list_item(series_name: str, entry: dict) -> dict:
    """前端集列表项：id/title/drama_name/total_seconds/style_override/word_count/status。

    word_count 取 source_chars；status 由产物存在性派生
    （script.json→scripted，plan.json→planned，否则 draft），仅表现层。
    """
    _series, _planner, _chars = _require_series_mods()
    e = dict(entry) if isinstance(entry, dict) else {}
    eid = str(e.get("id", "") or "")
    status = str(e.get("status", "") or "").strip()
    if not status:
        try:
            edir = _series.episode_dir(series_name, eid) if eid else None
            if edir is not None and (edir / "script.json").is_file():
                status = "scripted"
            elif edir is not None and (edir / "plan.json").is_file():
                status = "planned"
            else:
                status = "draft"
        except (ValueError, OSError):
            status = "draft"
    try:
        _traw = e.get("total_seconds", None)
        total = None if _traw in (None, "") else float(_traw)
    except (TypeError, ValueError):
        total = None
    try:
        wc = int(e.get("word_count", e.get("source_chars", 0)) or 0)
    except (TypeError, ValueError):
        wc = 0
    return {
        "id": eid,
        "title": str(e.get("title", "") or eid),
        "drama_name": str(e.get("drama_name", "") or ""),
        "total_seconds": total,
        "style_override": str(e.get("style_override", "") or ""),
        "word_count": wc,
        "status": status,
    }


def _character_portrait_url(series_name: str, rel: str) -> str:
    """立绘绝对 API URL：/api/series/{s}/files?path=<rel-urlencoded>。"""
    from urllib.parse import quote as _quote
    return (f"/api/series/{_quote(str(series_name), safe='')}"
            f"/files?path={_quote(str(rel), safe='')}")


def _present_character(series_name: str, c: dict) -> dict:
    """角色表现层：附加派生 desc（=logline）/ portrait_url / is_main 默认。

    只返回拷贝，不改落盘 schema（落盘仍 logline/images/is_main）。
    """
    d = dict(c) if isinstance(c, dict) else {}
    # desc 恒等于 logline（落盘无 desc 键，纯派生）
    try:
        d["desc"] = str(d.get("logline", "") or "")
    except Exception:
        d["desc"] = ""
    # 新字段透出（老记录缺键时回填默认，不改落盘）
    for _k, _default in (("relation", ""), ("outfit", ""),
                         ("status", "待确认")):
        try:
            _v = d.get(_k, "")
            if not isinstance(_v, str) or not _v.strip():
                d[_k] = _default
        except Exception:
            d[_k] = _default
    v = d.get("is_main", False)
    d["is_main"] = v if isinstance(v, bool) else False
    rel = ""
    try:
        imgs = d.get("images", [])
        if isinstance(imgs, list):
            for im in imgs:
                s = str(im or "").strip()
                if s:
                    rel = s
                    break
    except Exception:
        rel = ""
    d["portrait_url"] = _character_portrait_url(series_name, rel) if rel else ""
    return d


def _resolve_effective_style(series_data: dict, episode_id: str | None,
                             req_style: Any) -> str:
    """生效风格：req.style 非空即用，否则 ep.style_override，否则 series.style，否则 ""。"""
    if str(req_style or "").strip():
        return str(req_style or "").strip()
    if episode_id:
        _series, _planner, _chars = _require_series_mods()
        try:
            meta = _series.get_episode(
                str(series_data.get("name", "") or ""), episode_id)
            so = str((meta or {}).get("style_override", "") or "").strip()
            if so:
                return so
        except Exception:
            pass
    return str((series_data or {}).get("style", "") or "").strip()


@app.get("/api/series")
def series_list() -> dict:
    _series, _planner, _chars = _require_series_mods()
    try:
        items = _series.list_series()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"列出系列失败：{exc}") from exc
    return {"ok": True, "series": items, "count": len(items)}


@app.post("/api/series/new")
def series_new(req: SeriesNewRequest) -> dict:
    _series, _planner, _chars = _require_series_mods()
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="系列名不能为空")
    meta: dict[str, Any] = {}
    if req.title:
        meta["title"] = req.title
    if req.synopsis:
        meta["synopsis"] = req.synopsis
    if req.style:
        meta["style"] = req.style
    extra = req.model_dump() if hasattr(req, "model_dump") else dict(req)
    for k, v in (extra or {}).items():
        if k not in ("name", "title", "synopsis", "style") and k not in meta:
            meta[k] = v
    try:
        data = _series.create_series(name, meta=meta or None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "series": data}


@app.post("/api/series")
def series_create_alias(req: SeriesCreateAliasRequest) -> dict:
    """新建别名（前端实际调用）：name 为空时回退 title，否则 400；与 /new 同逻辑。"""
    _series, _planner, _chars = _require_series_mods()
    name = str(req.name or req.title or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="系列名不能为空（name/title 必填其一）")
    meta: dict[str, Any] = {}
    if req.title:
        meta["title"] = req.title
    if req.synopsis:
        meta["synopsis"] = req.synopsis
    if req.style:
        meta["style"] = req.style
    extra = req.model_dump() if hasattr(req, "model_dump") else dict(req)
    for k, v in (extra or {}).items():
        if k not in ("name", "title", "synopsis", "style") and k not in meta:
            meta[k] = v
    try:
        data = _series.create_series(name, meta=meta or None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "series": data}


@app.get("/api/series/{name}")
def series_get(name: str) -> dict:
    _series, _planner, _chars = _require_series_mods()
    try:
        data = _series.get_series(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        chars = _chars.load_characters(data.get("name", name))
    except Exception:
        chars = []
    return {"ok": True, "series": data, "characters": chars}


@app.delete("/api/series/{name}")
def series_delete(name: str) -> dict:
    _series, _planner, _chars = _require_series_mods()
    try:
        _series.delete_series(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "deleted": name}


def _extract_multipart_fallback(raw: bytes) -> bytes:
    """已废弃：缺 python-multipart 时不再静默取首段（会错解析），仅保留兼容。

    路由现直接 400 指引安装，本函数不再被调用。
    """
    try:
        if not raw:
            return b""
        # multipart 首段形如 --boundary\\r\\nheaders\\r\\n\\r\\nbody\\r\\n...
        parts = raw.split(b"\r\n\r\n", 1)
        if len(parts) == 2 and raw.lstrip().startswith(b"--"):
            body = parts[1]
            # 去尾 boundary
            idx = body.rfind(b"\r\n--")
            if idx >= 0:
                body = body[:idx]
            return body.strip(b"\r\n")
        return raw
    except Exception:
        return raw


@app.post("/api/series/{name}/episodes")
async def series_episode_create(name: str, request: _M1Request) -> dict:
    """新建分集：JSON（{title,total_seconds,source_text,style_override}）/ multipart（file md/txt）/ 原始字节。

    文件约束：md/txt、<=2MB、UTF-8 优先 GBK 回退；记 source_chars/hash/
    truncated，超 12000 字截断前部存 source.md。缺 python-multipart 时
    multipart 直接 400 指引安装（不再静默取首段，避免错解析）。
    style_override 三通道透传（JSON/multipart/query），strip 后 40 字截断。
    """
    _series, _planner, _chars = _require_series_mods()
    try:
        _series.get_series(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ctype = (request.headers.get("content-type") or "").lower()
    title = ""
    total_seconds: Any = None
    source_text = ""
    source_filename = ""
    style_override = ""

    def _check_bytes(filename: str, data: bytes) -> str:
        if filename and not filename.lower().endswith((".md", ".txt")):
            raise HTTPException(status_code=400, detail=f"仅支持 md/txt，当前 {filename!r}")
        if len(data or b"") > 2 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="文件超 2MB 上限")
        try:
            return _series.decode_upload_bytes(data or b"")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if "multipart" in ctype:
        try:
            form = await request.form()
        except AssertionError as exc:
            if "python-multipart" not in str(exc):
                raise
            raise HTTPException(
                status_code=400,
                detail="缺少 python-multipart：请 pip install python-multipart "
                       "后再用 multipart 上传（或改用 JSON source_text / 原始字节上传）",
            ) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"解析上传失败：{exc}") from exc
        parsed = await _parse_upload_form(form)
        title = parsed["title"]
        total_seconds = parsed["total_seconds"]
        source_text = parsed["source_text"]
        source_filename = parsed["source_filename"]
        style_override = parsed["style_override"]
    else:
        if ctype.startswith(("text/", "application/octet-stream")):
            qp = request.query_params
            title = str(qp.get("title", "") or "")
            try:
                _ts = qp.get("total_seconds", None)
                total_seconds = None if _ts in (None, "") else float(_ts)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="total_seconds 须为数字") from None
            source_filename = str(qp.get("filename", "") or qp.get("source_filename", "") or "")
            style_override = _parse_style_override(qp.get("style_override", ""))
            raw = await request.body()
            source_text = _check_bytes(source_filename, raw or b"")
            if not title and source_filename:
                title = source_filename.rsplit(".", 1)[0]
        else:
            try:
                body = await request.json()
            except Exception:
                body = {}
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="body 须为 JSON 对象")
            title = str(body.get("title", "") or "")
            total_seconds = body.get("total_seconds", None)
            source_text = str(body.get("source_text", "") or "")
            source_filename = str(body.get("source_filename", "") or body.get("filename", "") or "")
            style_override = _parse_style_override(body.get("style_override", ""))
    meta = _do_create_episode(name, title, total_seconds, source_text,
                              source_filename, style_override)
    return {"ok": True, "episode": meta}


@app.get("/api/series/{name}/episodes")
def series_episodes_list(name: str) -> dict:
    """集列表（前端实际调用）：每项含 id/title/drama_name/total_seconds/style_override/word_count/status。"""
    _series, _planner, _chars = _require_series_mods()
    try:
        items = _series.list_episodes(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "episodes": [_episode_list_item(name, e) for e in items],
            "count": len(items)}


@app.post("/api/series/{name}/episodes/upload")
async def series_episode_upload(name: str, request: _M1Request) -> dict:
    """加一集（文件 .md/.txt，前端实际调用）：FormData file + title? + style_override? + total_seconds?。

    与 episode-create 的 multipart 分支同逻辑（共用 _parse_upload_form）；
    缺 python-multipart 照现有 400 指引。非 multipart 则按 JSON 同字段受理。
    """
    _series, _planner, _chars = _require_series_mods()
    try:
        _series.get_series(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ctype = (request.headers.get("content-type") or "").lower()
    if "multipart" in ctype:
        try:
            form = await request.form()
        except AssertionError as exc:
            if "python-multipart" not in str(exc):
                raise
            raise HTTPException(
                status_code=400,
                detail="缺少 python-multipart：请 pip install python-multipart "
                       "后再用 multipart 上传（或改用 JSON source_text / 原始字节上传）",
            ) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"解析上传失败：{exc}") from exc
        parsed = await _parse_upload_form(form)
    else:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body 须为 JSON 对象")
        try:
            ts = body.get("total_seconds", None)
            total_f = None if ts in (None, "") else float(ts)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="total_seconds 须为数字") from None
        parsed = {
            "title": str(body.get("title", "") or ""),
            "total_seconds": total_f,
            "source_text": str(body.get("source_text", "") or ""),
            "source_filename": str(body.get("source_filename", "") or body.get("filename", "") or ""),
            "style_override": _parse_style_override(body.get("style_override", "")),
        }
    meta = _do_create_episode(name, parsed["title"], parsed["total_seconds"],
                              parsed["source_text"], parsed["source_filename"],
                              parsed["style_override"])
    return {"ok": True, "episode": meta}


@app.get("/api/series/{name}/episodes/{ep}")
def series_episode_get(name: str, ep: str) -> dict:
    _series, _planner, _chars = _require_series_mods()
    try:
        meta = _series.get_episode(name, ep)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    source = ""
    plan: Any = None
    try:
        source = _series.read_episode_source(name, meta.get("id", ep))
    except Exception:
        source = ""
    try:
        plan = _series.read_episode_plan(name, meta.get("id", ep))
    except Exception:
        plan = None
    return {"ok": True, "episode": meta, "source_preview": (source or "")[:500],
            "plan": plan}


@app.patch("/api/series/{name}/episodes/{ep}")
def series_episode_patch(name: str, ep: str, req: SeriesEpisodePatchRequest) -> dict:
    _series, _planner, _chars = _require_series_mods()
    patch: dict[str, Any] = {}
    if req.title is not None:
        patch["title"] = req.title
    if req.total_seconds is not None:
        try:
            v = float(req.total_seconds)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="total_seconds 须为数字") from None
        if v <= 0:
            raise HTTPException(status_code=400, detail=f"总时长须为正数，当前 {v}s")
        patch["total_seconds"] = v
    if req.source_text is not None:
        patch["source_text"] = req.source_text
    if req.style_override is not None:
        patch["style_override"] = req.style_override
    try:
        meta = _series.update_episode(name, ep, patch)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "episode": meta}


@app.delete("/api/series/{name}/episodes/{ep}")
def series_episode_delete(name: str, ep: str) -> dict:
    _series, _planner, _chars = _require_series_mods()
    try:
        _series.delete_episode(name, ep)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "deleted": ep}


@app.get("/api/series/{name}/characters")
def series_characters_list(name: str) -> dict:
    _series, _planner, _chars = _require_series_mods()
    try:
        _series.get_series(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        items = _chars.load_characters(name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取角色失败：{exc}") from exc
    shown = [_present_character(name, c) for c in items]
    return {"ok": True, "characters": shown, "count": len(shown)}


@app.post("/api/series/{name}/characters")
def series_character_create(name: str, req: CharacterCreateRequest) -> dict:
    """新建角色（前端实际调用）：body{name,desc}，desc 存入 logline。"""
    _series, _planner, _chars = _require_series_mods()
    try:
        _series.get_series(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    cname = str(req.name or "").strip()
    if not cname:
        raise HTTPException(status_code=400, detail="角色 name 不能为空")
    try:
        existing = _chars.load_characters(name)
    except Exception:
        existing = []
    try:
        cid = _chars.new_character_id(existing)
        norm = _chars.normalize_character({
            "id": cid, "name": cname,
            "logline": str(req.desc or "").strip(),
        })
        _chars.validate_character(norm)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    existing.append(norm)
    try:
        _chars.save_characters(name, existing)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "character": _present_character(name, norm)}


@app.delete("/api/series/{name}/characters/{cid}")
def series_character_delete(name: str, cid: str) -> dict:
    """删除角色（前端实际调用）：删后 save，返回 {ok,deleted}。"""
    _series, _planner, _chars = _require_series_mods()
    try:
        _series.get_series(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        items = _chars.load_characters(name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取角色失败：{exc}") from exc
    kept = [c for c in items
            if not (isinstance(c, dict) and str(c.get("id", "")) == cid)]
    if len(kept) == len(items):
        raise HTTPException(status_code=404, detail=f"角色 {cid!r} 不存在")
    try:
        _chars.save_characters(name, kept)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "deleted": cid}


@app.post("/api/series/{name}/characters/merge")
def series_characters_merge(name: str, req: CharacterMergeRequest) -> dict:
    """合并角色（前端实际调用）：sources 的 aliases/images 并入 target（去重）。

    logline/relation/outfit 取 target 非空即留，否则取首个非空 source；
    status 粘性与 characters.merge_characters 对齐：target 已确认保持
    （不被待确认回退）；target 待确认（或空）且任一 source 已确认 →
    升级 target 为已确认。删 sources 后 save；target 不存在/空来源 400。
    """
    _series, _planner, _chars = _require_series_mods()
    try:
        _series.get_series(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    target_id = str(req.target_id or "").strip()
    source_ids = [str(s or "").strip() for s in (req.source_ids or []) if str(s or "").strip()]
    source_ids = [s for s in source_ids if s != target_id]
    if not target_id:
        raise HTTPException(status_code=400, detail="target_id 不能为空")
    if not source_ids:
        raise HTTPException(status_code=400, detail="source_ids 不能为空")
    try:
        items = _chars.load_characters(name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取角色失败：{exc}") from exc
    by_id = {str(c.get("id", "")): c for c in items if isinstance(c, dict)}
    if target_id not in by_id:
        raise HTTPException(status_code=400, detail=f"目标角色 {target_id!r} 不存在")
    target = dict(by_id[target_id])
    deleted: list[str] = []
    for sid in source_ids:
        src = by_id.get(sid)
        if src is None:
            continue
        for a in (src.get("aliases", []) or []):
            s = str(a or "").strip()
            if s and s != target.get("name") and s not in (target.get("aliases", []) or []):
                target.setdefault("aliases", []).append(s)
        for im in (src.get("images", []) or []):
            s = str(im or "").strip()
            if s and s not in (target.get("images", []) or []):
                target.setdefault("images", []).append(s)
        if not str(target.get("logline", "") or "").strip():
            v = str(src.get("logline", "") or "").strip()
            if v:
                target["logline"] = v
        for _f in ("relation", "outfit"):
            if not str(target.get(_f, "") or "").strip():
                _v = str(src.get(_f, "") or "").strip()
                if _v:
                    target[_f] = _v
        # status 粘性（与 characters.merge_characters 对齐）：target 已确认
        # 保持不回退；target 待确认/空且 source 已确认 → 升级为已确认。
        if (str(target.get("status", "") or "").strip() != "已确认"
                and str(src.get("status", "") or "").strip() == "已确认"):
            target["status"] = "已确认"
        deleted.append(sid)
    if not deleted:
        raise HTTPException(status_code=400, detail="来源角色均不存在（无可合并项）")
    if target.get("is_main"):
        for c in items:
            if isinstance(c, dict) and str(c.get("id", "")) not in (target_id, *deleted):
                c["is_main"] = False
    kept = [c for c in items
            if not (isinstance(c, dict) and str(c.get("id", "")) in set(deleted))]
    for i, c in enumerate(kept):
        if isinstance(c, dict) and str(c.get("id", "")) == target_id:
            kept[i] = target
            break
    try:
        norm = _chars.normalize_character(target)
        _chars.validate_character(norm)
        kept = [norm if (isinstance(c, dict) and str(c.get("id", "")) == target_id) else c
                for c in kept]
        _chars.save_characters(name, kept)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "character": _present_character(name, norm),
            "target_id": target_id, "deleted": deleted}


@app.post("/api/series/{name}/characters/extract")
def series_characters_extract(name: str, req: CharacterExtractRequest) -> dict:
    """LLM 抽取角色 + 增量合并（locked 不覆盖，同名/别名合并）。

    单条坏数据跳过并计 skipped（响应透出）；LLM 整体无效时用该集 plan.json
    的 cast_plan 建占位角色（仅 name+first_seen，其余空，status 待确认），
    有占位返 200 + note，两者皆无才 502。
    """
    _series, _planner, _chars = _require_series_mods()
    try:
        _series.get_series(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    source = (req.source_text or "").strip()
    if not source and req.episode_id:
        try:
            source = _series.read_episode_source(name, req.episode_id)
        except Exception:
            source = ""
    if not source.strip():
        raise HTTPException(status_code=400, detail="请提供原文（source_text 或 episode_id）")
    try:
        existing = _chars.load_characters(name)
    except Exception:
        existing = []
    summary = ""
    try:
        # 角色摘要注入抽取提示：summarize 保证每角色至少一行（超限降级
        # 紧凑行+尾注“共 N 个角色，已列 M 个”），此处不再二次截断。
        summary = _chars.summarize_characters(existing)
    except Exception:
        summary = ""
    system, user = _chars.build_character_prompt(source, summary)
    cfg = _text_cfg()
    result, latency_ms = _run_text_with_config(cfg, user, system=system)
    first_seen = (req.first_seen or req.episode_id or "")
    try:
        raw = _breakdown.extract_json(result.get("text", "")) if _breakdown is not None else json.loads(result.get("text", ""))
        if hasattr(_chars, "coerce_characters_with_skipped"):
            fresh, skipped = _chars.coerce_characters_with_skipped(
                raw, first_seen=first_seen)
        else:  # 兼容旧 characters 模块：总数差即 skipped
            total_items = len(raw.get("characters", [])) if isinstance(raw, dict) else 0
            fresh = _chars.coerce_characters(raw, first_seen=first_seen)
            skipped = max(0, int(total_items) - len(fresh))
        # G1 逐字 grounding：coerce 后、merge 前对齐原文原形
        if hasattr(_chars, "canonicalize_names"):
            grounded, skipped_ground = _chars.canonicalize_names(fresh, source)
        else:
            grounded, skipped_ground = list(fresh), 0
        skipped = int(skipped) + int(skipped_ground)
        merged = _chars.merge_characters(existing, grounded)
        _chars.save_characters(name, merged)
    except ValueError as exc:
        # 识别兜底：LLM 无效 → plan cast_plan 占位（走正常 merge+save）
        ep_id = str(req.episode_id or req.first_seen or "").strip()
        cast_names: list[str] = []
        if ep_id:
            try:
                _plan = _series.read_episode_plan(name, ep_id)
            except Exception:
                _plan = None
            if isinstance(_plan, dict):
                for _entry in (_plan.get("cast_plan", []) or []):
                    _nm = str((_entry or {}).get("name", "") or "").strip() \
                        if isinstance(_entry, dict) else str(_entry or "").strip()
                    if _nm and _nm not in cast_names:
                        cast_names.append(_nm)
        placeholders = [{"name": _nm, "first_seen": ep_id,
                         "status": "待确认"} for _nm in cast_names]
        # 占位名同样逐字 grounding：不在原文的编造名拦在库外（全拦则走 502）。
        if placeholders and hasattr(_chars, "canonicalize_names"):
            try:
                _grounded_ph, _ = _chars.canonicalize_names(placeholders, source)
                placeholders = [{"name": str(_g.get("name", "") or "").strip(),
                                 "first_seen": ep_id, "status": "待确认"}
                                for _g in _grounded
                                if str(_g.get("name", "") or "").strip()]
            except Exception:
                pass
        if not placeholders:
            raise HTTPException(
                status_code=502,
                detail=f"角色抽取结果未通过校验：{exc}；"
                       f"且 plan 无 cast_plan 可回退（{ep_id or '未指定分集'}）") from exc
        try:
            merged = _chars.merge_characters(existing, placeholders)
            _chars.save_characters(name, merged)
        except ValueError as exc2:
            raise HTTPException(status_code=502, detail=f"占位角色落盘失败：{exc2}") from exc2
        _old_ids = {str(c.get("id", "")) for c in (existing or []) if isinstance(c, dict)}
        _added = [str(c.get("name", "")) for c in merged
                  if isinstance(c, dict) and str(c.get("id", "")) not in _old_ids]
        return {"ok": True,
                "characters": [_present_character(name, c) for c in merged],
                "count": len(merged),
                "skipped": 0,
                "generated_by": result.get("generated_by", ""),
                "latency_ms": latency_ms,
                "note": f"LLM 抽取无效（{exc}），已按 plan cast_plan 建"
                        f"{len(placeholders)} 个占位角色（待确认），请核对后确认；"
                        f"新增{len(_added)}（{','.join(_added[:5])}），"
                        f"沿用{len(merged) - len(_added)}"}
    _old_ids = {str(c.get("id", "")) for c in (existing or []) if isinstance(c, dict)}
    _added = [str(c.get("name", "")) for c in merged
              if isinstance(c, dict) and str(c.get("id", "")) not in _old_ids]
    return {"ok": True,
            "characters": [_present_character(name, c) for c in merged],
            "count": len(merged),
            "skipped": skipped,
            "generated_by": result.get("generated_by", ""), "latency_ms": latency_ms,
            "note": f"grounded {len(grounded)} 条，skipped {skipped} 条"
                    f"（原文逐字 grounding）；新增{len(_added)}"
                    f"{('（' + ','.join(_added[:5]) + '）') if _added else ''}，"
                    f"沿用{len(merged) - len(_added)}"}


@app.patch("/api/series/{name}/characters/{cid}")
def series_character_patch(name: str, cid: str, req: dict) -> dict:
    _series, _planner, _chars = _require_series_mods()
    try:
        items = _chars.load_characters(name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取角色失败：{exc}") from exc
    if not isinstance(req, dict):
        raise HTTPException(status_code=400, detail="body 须为 JSON 对象")
    idx = -1
    for i, c in enumerate(items):
        if isinstance(c, dict) and str(c.get("id", "")) == cid:
            idx = i
            break
    if idx < 0:
        raise HTTPException(status_code=404, detail=f"角色 {cid!r} 不存在")
    cur = dict(items[idx])
    for k in ("name", "aliases", "logline", "appearance", "personality",
              "relation", "outfit", "status",
              "images", "voice", "first_seen", "notes", "locked", "is_main"):
        if k in req:
            cur[k] = req[k]
    if "desc" in req and "logline" not in req:
        cur["logline"] = req["desc"]
    try:
        norm = _chars.normalize_character(cur)
        _chars.validate_character(norm)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    items[idx] = norm
    if norm.get("is_main"):
        # 单主图：同系列其他角色 is_main 置 false
        for j, c in enumerate(items):
            if j != idx and isinstance(c, dict) and c.get("is_main"):
                c["is_main"] = False
    try:
        _chars.save_characters(name, items)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "character": _present_character(name, norm)}


def _do_series_plan(name: str, req: SeriesPlanRequest) -> dict:
    """Planner 核心（/plan 与 /episodes/{ep}/plan 共用）：原文 → plan.json 落盘。

    生效风格 = req.style 非空即用，否则 ep.style_override，否则 series.style。
    total_seconds 可空：显式传入为 manual；空（None）则 AI 按原文信息量
    定时长（total_source="ai"），plan 落盘后经 update_episode 回写
    episode.total_seconds（触发系列总量重算）。
    """
    _series, _planner, _chars = _require_series_mods()
    if _breakdown is None and _M1_planner is None:
        raise HTTPException(status_code=503, detail="规划模块缺失")
    try:
        series_data = _series.get_series(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    ep_id = (req.episode_id or "").strip() or None
    source = (req.source_text or "").strip()
    total_req: Any = req.total_seconds
    if isinstance(total_req, str) and not total_req.strip():
        total_req = None
    is_explicit = total_req is not None
    if not source and ep_id:
        try:
            source = _series.read_episode_source(name, ep_id)
        except Exception:
            source = ""
    if not (source or "").strip():
        raise HTTPException(status_code=400, detail="请提供原文（source_text 或 episode_id）")
    style = _resolve_effective_style(series_data, ep_id, req.style)
    chars_summary = (req.characters_summary or "").strip()
    lib_chars: list = []
    try:
        lib_chars = _chars.load_characters(name) or []
    except Exception:
        lib_chars = []
    if not chars_summary:
        try:
            # Planner 角色锚点：summarize 超限降级每角色一行+尾注，不断尾丢人。
            chars_summary = _chars.summarize_characters(lib_chars)
        except Exception:
            chars_summary = ""
    # 人名 grounding 与模型所见原文对齐（prompt 截断前 12000 字）。
    ground_src = source[:12000] if len(source) > 12000 else source
    prev = (req.prev_summary or "").strip()
    if not prev and ep_id:
        try:
            prev = _series.prev_episode_summary(name, ep_id)
        except Exception:
            prev = ""
    ep_title = ""
    if ep_id:
        try:
            ep_title = str(_series.get_episode(name, ep_id).get("title", "") or "")
        except Exception:
            ep_title = ep_id
    cfg = _text_cfg()
    if not is_explicit:
        # AI 定时长：prompt 以 None 提议 total
        system, user = _planner.build_planner_prompt(
            series_data.get("name", name), None, style, source,
            characters_summary=chars_summary, prev_summary=prev,
            episode_title=ep_title)
        result, latency_ms = _run_text_with_config(cfg, user, system=system)
        try:
            raw = _planner.parse_plan_json(result.get("text", ""))
            plan = _planner.coerce_plan(raw, None, style,
                                        source_text=ground_src,
                                        library_names=lib_chars or None)
            plan["generated_by"] = result.get("generated_by", "")
        except ValueError as exc:
            raise HTTPException(status_code=502, detail=f"规划结果未通过校验：{exc}") from exc
        total_source = "ai"
    else:
        try:
            total_f = float(total_req)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="total_seconds 须为数字") from None
        if total_f <= 0:
            raise HTTPException(status_code=400, detail=f"总时长须为正数，当前 {total_f}s")
        system, user = _planner.build_planner_prompt(
            series_data.get("name", name), total_f, style, source,
            characters_summary=chars_summary, prev_summary=prev,
            episode_title=ep_title)
        result, latency_ms = _run_text_with_config(cfg, user, system=system)
        try:
            raw = _planner.parse_plan_json(result.get("text", ""))
            plan = _planner.coerce_plan(raw, total_f, style,
                                        source_text=ground_src,
                                        library_names=lib_chars or None)
            plan["generated_by"] = result.get("generated_by", "")
        except ValueError as exc:
            raise HTTPException(status_code=502, detail=f"规划结果未通过校验：{exc}") from exc
        total_source = "manual"
    if ep_id:
        try:
            _series.write_episode_plan(name, ep_id, plan)
        except (ValueError, FileNotFoundError, OSError) as exc:
            raise HTTPException(status_code=400, detail=f"落盘 plan.json 失败：{exc}") from exc
        try:
            _series.update_episode(name, ep_id,
                                   {"total_seconds": float(plan.get("total_seconds", 0))})
        except (ValueError, FileNotFoundError, OSError) as exc:
            raise HTTPException(status_code=400, detail=f"回写分集总时长失败：{exc}") from exc
    return {"ok": True, "plan": plan, "generated_by": result.get("generated_by", ""),
            "latency_ms": latency_ms, "total_source": total_source}


@app.post("/api/series/{name}/plan")
def series_plan(name: str, req: SeriesPlanRequest) -> dict:
    """Planner：原文 → {total/clip_durations/style/cast/market/density}，落 plan.json。"""
    return _do_series_plan(name, req)


@app.post("/api/series/{name}/episodes/{ep}/plan")
def series_episode_plan(name: str, ep: str, req: dict | None = None) -> dict:
    """每集规划（前端实际调用）：薄封装，等价 SeriesPlanRequest{episode_id:ep} 走同一核心。

    total_seconds 可空（None 即 AI 定时长并回写分集）；其余 style/source_text/
    characters_summary/prev_summary 透传。
    """
    body = req if isinstance(req, dict) else {}
    try:
        eid = _require_series_mods()[0].normalize_episode_id(ep)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _total: Any = body.get("total_seconds", None) if isinstance(body, dict) else None
    if isinstance(_total, str) and not _total.strip():
        _total = None
    if _total is not None:
        try:
            _total = float(_total)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="total_seconds 须为数字") from None
    return _do_series_plan(name, SeriesPlanRequest(
        episode_id=eid,
        total_seconds=_total,
        style=str(body.get("style", "") or "") if isinstance(body, dict) else "",
        source_text=str(body.get("source_text", "") or "") if isinstance(body, dict) else "",
        characters_summary=str(body.get("characters_summary", "") or "") if isinstance(body, dict) else "",
        prev_summary=str(body.get("prev_summary", "") or "") if isinstance(body, dict) else "",
    ))


def _fill_script_character_refs(script: dict, lib_chars: list,
                                  lib_scenes: list | None = None) -> dict:
    """分镜 script 接入角色库 + 场景库：填 character_refs/scene_refs + 提示词注入。

    - character_refs：本集出场（各镜 cast 并集）优先、is_main 居前，
      每角色取主图 images[0]，≤5（模型硬约束；库本身不限）；
    - scene_refs：本集出场（各镜 scene 并集）优先，每场景取主图 images[0]，≤5；
    - image_prompt：按镜 cast 注入 appearance 短描（单角色 60 字、分号连、
      总长 200 字封顶）+ 按镜 scene 注入 description 短描（[场景]段，不破坏六段式）；
      双库为空则只保底 refs=[]/scene_refs=[]。
    """
    chars_mod = globals().get("_M1_characters")
    bd_mod = globals().get("_breakdown")
    clips = script.get("clips", []) if isinstance(script, dict) else []
    if not isinstance(clips, list):
        clips = []
    # 各镜 cast 并集（保序去重，作为本集出场表）
    cast_all: list[str] = []
    for _c in clips:
        if not isinstance(_c, dict):
            continue
        _cast = _c.get("cast", [])
        if not isinstance(_cast, list):
            continue
        for _n in _cast:
            _s = str(_n or "").strip()
            if _s and _s not in cast_all:
                cast_all.append(_s)
    libs = [c for c in (lib_chars or []) if isinstance(c, dict)]
    # 角色键→库条目（名/别名归一，别名命中即合并的同一规则）
    import re as _re

    def _nk(s: str) -> str:
        try:
            return _re.sub(r"\s+", "", str(s or "").strip().lower())
        except Exception:
            return str(s or "").strip().lower()

    key_to_char: dict[str, dict] = {}
    if chars_mod is not None and hasattr(chars_mod, "char_keys"):
        for _c in libs:
            try:
                _keys = chars_mod.char_keys(_c)
            except Exception:
                _keys = set()
            for _k in (_keys or set()):
                key_to_char.setdefault(str(_k), _c)
    # 各镜 scene 并集（保序去重，作为本集出场场景表）
    scene_all: list[str] = []
    for _c in clips:
        if not isinstance(_c, dict):
            continue
        _sc = _c.get("scene", [])
        if isinstance(_sc, str):
            _sc = [_sc]
        if not isinstance(_sc, list):
            continue
        for _n in _sc:
            _s = str(_n or "").strip()
            if _s and _s not in scene_all:
                scene_all.append(_s)
    slibs = [c for c in (lib_scenes or []) if isinstance(c, dict)]
    scenes_mod = globals().get("_M1_scenes")
    if scenes_mod is not None and hasattr(scenes_mod, "select_scene_refs"):
        try:
            script["scene_refs"] = list(
                scenes_mod.select_scene_refs(slibs, scene_all, 5))
        except Exception:
            script["scene_refs"] = []
    else:
        _srefs: list[str] = []
        for _c in slibs:
            _imgs = _c.get("images", []) if isinstance(_c, dict) else []
            if isinstance(_imgs, list):
                for _im in _imgs:
                    _s = str(_im or "").strip()
                    if _s and _s not in _srefs:
                        _srefs.append(_s)
                        break
            if len(_srefs) >= 5:
                break
        script["scene_refs"] = _srefs[:5]
    if not libs:
        script["character_refs"] = []
    if chars_mod is not None and hasattr(chars_mod, "select_character_refs"):
        try:
            script["character_refs"] = list(
                chars_mod.select_character_refs(libs, cast_all, 5))
        except Exception:
            script["character_refs"] = []
    else:  # 兼容旧 characters 模块：is_main 居前取主图 ≤5
        _refs: list[str] = []
        _ordered = sorted(libs, key=lambda c: 0 if c.get("is_main") else 1)
        for _c in _ordered:
            _imgs = _c.get("images", [])
            if isinstance(_imgs, list):
                for _im in _imgs:
                    _s = str(_im or "").strip()
                    if _s and _s not in _refs:
                        _refs.append(_s)
                        break
            if len(_refs) >= 5:
                break
        script["character_refs"] = _refs[:5]
    # image_prompt[主体]注入：按镜 cast 查库 appearance
    _name_to_char: dict[str, dict] = {}
    for _c in libs:
        _nm = str(_c.get("name", "") or "").strip()
        if _nm and _nm not in _name_to_char:
            _name_to_char[_nm] = _c
    _inject = getattr(bd_mod, "inject_cast_appearance", None) if bd_mod else None
    _per_max = int(getattr(bd_mod, "CAST_APPEARANCE_PER_MAX", 60) or 60) \
        if bd_mod else 60
    if _inject is not None:
        for _clip in clips:
            if not isinstance(_clip, dict):
                continue
            _cast = _clip.get("cast", [])
            if not isinstance(_cast, list) or not _cast:
                continue
            _seen: set[str] = set()
            _descs: list[str] = []
            for _n in _cast:
                _s = str(_n or "").strip()
                if not _s or _s in _seen:
                    continue
                _seen.add(_s)
                _hit = key_to_char.get(_nk(_s)) or _name_to_char.get(_s)
                if not isinstance(_hit, dict):
                    continue
                _app = str(_hit.get("appearance", "") or "").strip()
                if not _app:
                    continue
                _descs.append(f"{_s}：{_app[:_per_max]}")
            if _descs:
                try:
                    _clip["image_prompt"] = _inject(
                        str(_clip.get("image_prompt", "") or ""), _descs)
                except Exception:
                    pass
    # image_prompt[场景]注入：按镜 scene 查库 description
    _name_to_scene: dict[str, dict] = {}
    for _c in slibs:
        _nm = str(_c.get("name", "") or "").strip()
        if _nm and _nm not in _name_to_scene:
            _name_to_scene[_nm] = _c
    _inject_sc = getattr(bd_mod, "inject_scene_description", None) if bd_mod else None
    _sc_max = int(getattr(bd_mod, "SCENE_DESC_PER_MAX", 60) or 60) \
        if bd_mod else 60
    if _inject_sc is not None:
        for _clip in clips:
            if not isinstance(_clip, dict):
                continue
            _sc = _clip.get("scene", [])
            if isinstance(_sc, str):
                _sc = [_sc]
            if not isinstance(_sc, list) or not _sc:
                continue
            _seen2: set[str] = set()
            _sdescs: list[str] = []
            for _n in _sc:
                _s = str(_n or "").strip()
                if not _s or _s in _seen2:
                    continue
                _seen2.add(_s)
                _hit = _name_to_scene.get(_s)
                if not isinstance(_hit, dict):
                    continue
                _dd = str(_hit.get("description", "") or "").strip()
                if not _dd:
                    continue
                _sdescs.append(f"{_s}：{_dd[:_sc_max]}")
            if _sdescs:
                try:
                    _clip["image_prompt"] = _inject_sc(
                        str(_clip.get("image_prompt", "") or ""), _sdescs)
                except Exception:
                    pass
    return script


def _normalize_breakdown_speakers(script: dict, lib_chars: list,
                                  source_text: str = "") -> int:
    """G3 分镜 speaker/cast 归一（路由层，coerce 后调用）。

    - speaker 在库名/aliases 中（大小写敏感、strip 精确）→映射为库 name；
    - 在原文出现过但不在库→保留原字并追加进该镜 cast（供下次识别收编）；
    - 既不在库也不在原文→改“旁白”并计数返回（计入 note）。
    cast 数组保留原文称呼原样（仅第二分支追加），runner 侧按名/别名
    精确匹配、找不到跳过不崩。空 speaker 与“旁白”原样保留，不计数。
    """
    src = source_text or ""
    alias_to_name: dict[str, str] = {}
    for c in (lib_chars or []):
        if not isinstance(c, dict):
            continue
        nm = str(c.get("name", "") or "").strip()
        if not nm:
            continue
        alias_to_name.setdefault(nm, nm)
        aliases = c.get("aliases", [])
        if isinstance(aliases, list):
            for a in aliases:
                s = str(a or "").strip()
                if s:
                    alias_to_name.setdefault(s, nm)
    clips = script.get("clips", []) if isinstance(script, dict) else []
    if not isinstance(clips, list):
        return 0
    fallback = 0
    for clip in clips:
        if not isinstance(clip, dict):
            continue
        sp = str(clip.get("speaker", "") or "").strip()
        if not sp or sp == "旁白":
            clip["speaker"] = sp
            continue
        if sp in alias_to_name:
            clip["speaker"] = alias_to_name[sp]
            continue
        if sp and sp in src:
            # 保留原字，追加进该镜 cast 供下次收编
            cast = clip.get("cast", [])
            if not isinstance(cast, list):
                cast = []
                clip["cast"] = cast
            if sp not in [str(x or "").strip() for x in cast]:
                cast.append(sp)
            clip["speaker"] = sp
            continue
        clip["speaker"] = "旁白"
        fallback += 1
    return fallback


def _reconcile_script_cast(script: dict, lib_chars: list) -> tuple[int, int]:
    """分镜 cast[] 对库（路由层，speaker 归一后、refs 回填前调用）。

    每镜 cast[] 逐项过库名/别名表（大小写敏感、strip 精确，与
    _normalize_breakdown_speakers 同口径）：命中别名改写为库本名；
    未命中**保留原文写法原样**（Q3 保留决策，供去角色页确认/合并）。
    库为空时直接 (0, 0) 不打扰。返回 (改写处数, 保留未命中处数)。
    """
    alias_to_name: dict[str, str] = {}
    for c in (lib_chars or []):
        if not isinstance(c, dict):
            continue
        nm = str(c.get("name", "") or "").strip()
        if not nm:
            continue
        alias_to_name.setdefault(nm, nm)
        aliases = c.get("aliases", [])
        if isinstance(aliases, list):
            for a in aliases:
                s = str(a or "").strip()
                if s:
                    alias_to_name.setdefault(s, nm)
    if not alias_to_name:
        return 0, 0
    clips = script.get("clips", []) if isinstance(script, dict) else []
    if not isinstance(clips, list):
        return 0, 0
    remapped = 0
    kept = 0
    for clip in clips:
        if not isinstance(clip, dict):
            continue
        cast = clip.get("cast", [])
        if not isinstance(cast, list):
            continue
        for i, entry in enumerate(cast):
            s = str(entry or "").strip()
            if not s:
                continue
            if s in alias_to_name:
                canon = alias_to_name[s]
                if canon != s:
                    cast[i] = canon
                    remapped += 1
            else:
                kept += 1
    return remapped, kept


@app.post("/api/series/{name}/episodes/{ep}/breakdown")
def series_episode_breakdown(name: str, ep: str, req: SeriesBreakdownRequest) -> dict:
    """分集 AI 分镜：用 plan 可变表 + 角色摘要 + 前集梗概（<=300字）出初稿并落 script.json。

    clip_seconds 可选：None/"" 时有 plan 用 plan 表、无 plan 自动规划；
    仅显式传 4-12 且无 plan 时才均匀切分。无 plan.json（且需 plan 表）时
    自动先跑 planner（复用 _do_series_plan）。
    """
    _series, _planner, _chars = _require_series_mods()
    if _breakdown is None:
        raise HTTPException(status_code=503, detail="AI 分镜不可用（breakdown 模块缺失）")
    aspect = (req.aspect or "9:16").strip()
    if aspect not in ("9:16", "16:9"):
        raise HTTPException(status_code=400, detail=f"画幅非法：{aspect!r}（仅 9:16 / 16:9）")
    raw_clip: Any = req.clip_seconds
    is_explicit_clip = not (
        raw_clip is None or (isinstance(raw_clip, str) and not raw_clip.strip()))
    if is_explicit_clip:
        per = str(raw_clip).strip()
        if per not in {str(i) for i in range(4, 13)}:
            raise HTTPException(status_code=400, detail=f"单镜时长须为 4-12（字符串），当前 {req.clip_seconds!r}")
    else:
        per = "8"
    try:
        meta = _series.get_episode(name, ep)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    eid = str(meta.get("id", ep))
    source = (req.source_text or "").strip() or _series.read_episode_source(name, eid)
    if not (source or "").strip():
        raise HTTPException(status_code=400, detail="分集无原文：请先上传 md/txt 或在 body 带 source_text")
    # meta 总时长：缺（None/""）则视为缺失（自动规划时 AI 定并回写）
    _mt_raw = meta.get("total_seconds", None)
    if _mt_raw is None or (isinstance(_mt_raw, str) and not str(_mt_raw).strip()):
        meta_total: float | None = None
    else:
        try:
            meta_total = float(_mt_raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="分集总时长须为数字") from None
        if meta_total <= 0:
            raise HTTPException(status_code=400, detail=f"总时长须为正数，当前 {meta_total}s")
    durations = None
    style_eff = None
    try:
        plan = _series.read_episode_plan(name, eid)
    except Exception:
        plan = None
    total: float
    if isinstance(plan, dict):
        # 有 plan：total 取 meta（缺则取 plan），校验 plan 表后采用
        if meta_total is not None:
            total = float(meta_total)
        else:
            try:
                total = float(plan.get("total_seconds", 0) or 0)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="plan 缺少合法 total_seconds") from None
            if total <= 0:
                raise HTTPException(status_code=400, detail="plan 缺少合法 total_seconds") from None
        durs = plan.get("clip_durations")
        if isinstance(durs, list) and durs:
            # plan 非法镜表拒绝透传：逐镜 4-12 且求和==total，否则 400
            try:
                if hasattr(_breakdown, "_validate_durations"):
                    _breakdown._validate_durations(list(durs), total)
                else:  # 兼容旧 breakdown：本地兜底校验
                    for i, _d in enumerate(durs):
                        _f = float(_d)
                        if not (4 <= _f <= 12):
                            raise ValueError(f"durations[{i}]={_d!r}须在4-12内")
                    if abs(sum(float(_d) for _d in durs) - total) > 1e-6:
                        raise ValueError("durations求和!=total")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"plan 非法：{exc}") from exc
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=f"plan 非法：durations须为数字数组（{exc}）") from exc
            durations = durs
        se = str(plan.get("style_effective", "") or "").strip()
        style_eff = se or None
    else:
        # 无 plan.json：显式 4-12 且 meta 有 total → 均匀切分（旧行为）；
        # 否则自动先跑 planner（复用 _do_series_plan），再走分镜逻辑。
        if is_explicit_clip and meta_total is not None:
            total = float(meta_total)
            durations = None
        else:
            auto_req = SeriesPlanRequest(
                episode_id=eid,
                total_seconds=float(meta_total) if meta_total is not None else None,
                style=str(req.style or "").strip(),
                source_text=str(req.source_text or "").strip(),
            )
            # 文本未配/失败保持 400/502 可操作信息，不吞异常（直接透传）
            auto_res = _do_series_plan(name, auto_req)
            plan = auto_res.get("plan")
            if not isinstance(plan, dict):
                raise HTTPException(status_code=502, detail="自动规划未返回合法 plan")
            try:
                total = float(plan.get("total_seconds", 0) or 0)
            except (TypeError, ValueError):
                raise HTTPException(status_code=502, detail="自动规划 total 非法") from None
            _durs = plan.get("clip_durations")
            if isinstance(_durs, list) and _durs:
                durations = _durs
            se = str(plan.get("style_effective", "") or "").strip()
            style_eff = se or None
    chars_summary = ""
    lib_chars: list = []
    try:
        lib_chars = _chars.load_characters(name)
        # 分镜角色锚点：summarize 超限降级每角色一行+尾注，不断尾丢人。
        chars_summary = _chars.summarize_characters(lib_chars)
    except Exception:
        lib_chars = []
        chars_summary = ""
    # 场景库：摘要 + 资产清单（失败降级为空，不阻断分镜）
    lib_scenes = _load_scene_lib(name)
    scenes_summary = ""
    _sm = _scenes_mod()
    if _sm is not None:
        try:
            scenes_summary = _sm.summarize_scenes(lib_scenes)
        except Exception:
            scenes_summary = ""
    asset = _series_assets(name)
    asset_lines = str(asset.get("asset_lines", "") or "")
    available = asset.get("available", set())
    # plan.cast_plan 出场名表（prompt 每镜 cast[] 用；无 plan 则空）
    plan_cast: list[str] = []
    if isinstance(plan, dict):
        for _entry in (plan.get("cast_plan", []) or []):
            _nm = str((_entry or {}).get("name", "") or "").strip() \
                if isinstance(_entry, dict) else str(_entry or "").strip()
            if _nm and _nm not in plan_cast:
                plan_cast.append(_nm)
    try:
        prev = _series.prev_episode_summary(name, eid)
    except Exception:
        prev = ""
    style = (req.style or "").strip() or str(meta.get("style_override", "") or "").strip()
    try:
        system, user = _breakdown.build_breakdown_prompt(
            str(meta.get("title", eid) or eid), total, aspect, per, style, source,
            durations=durations, style_effective=style_eff,
            characters_summary=chars_summary, prev_summary=prev,
            cast_plan=plan_cast,
            scenes_summary=scenes_summary, asset_lines=asset_lines,
            scene_plan=None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"plan 非法：{exc}") from exc
    cfg = _text_cfg()
    result, latency_ms = _run_text_with_config(cfg, user, system=system)
    try:
        raw = _breakdown.extract_json(result.get("text", ""))
        script = _breakdown.coerce_script(raw, str(meta.get("title", eid) or eid),
                                          total, aspect, per, style,
                                          durations=durations,
                                          style_effective=style_eff)
        script["generated_by"] = result.get("generated_by", "")
        speaker_fallback = _normalize_breakdown_speakers(script, lib_chars, source)
        cast_remapped, cast_kept = _reconcile_script_cast(script, lib_chars)
        _fill_script_character_refs(script, lib_chars, lib_scenes)
        _validate_script(script)
        try:
            _find_missing = getattr(_breakdown, "find_missing_assets", None)
            missing = _find_missing(script, set(available or set())) \
                if callable(_find_missing) else []
        except Exception:
            missing = []
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"AI 分镜结果未通过校验：{exc}") from exc
    try:
        _series.write_episode_script(name, eid, script)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=500, detail=f"落盘 script.json 失败：{exc}") from exc
    # 镜像到老单剧命名空间：分镜表 ?name= / 队列 / 渲染 / SSE 全部复用现有通道
    drama_name = str(meta.get("drama_name", "") or "").strip()
    if not drama_name:
        try:
            drama_name = _series.episode_drama_name(name, eid)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        _create_drama(drama_name, script)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"同步单剧命名空间失败：{exc}") from exc
    note_parts = [f"speaker 归一：{speaker_fallback} 镜回落旁白"]
    if cast_remapped:
        note_parts.append(f"出场对齐：{cast_remapped} 处别名已改库本名")
    if cast_kept:
        note_parts.append(
            f"{cast_kept} 处出场名未命中角色库（已保留原文写法，请去角色页确认/合并）")
    if isinstance(missing, list) and missing:
        kinds = {}
        for m in missing:
            if isinstance(m, dict):
                k = str(m.get("kind", "") or "")
                kinds[k] = kinds.get(k, 0) + 1
        part = "、".join(f"{k}{v}处" for k, v in kinds.items() if k) or f"{len(missing)}处"
        note_parts.append(f"待补图{len(missing)}处（{part}）：可在分镜表补传或 AI 生成")
    return {"ok": True, "script": script, "episode_id": eid,
            "drama_name": drama_name,
            "generated_by": result.get("generated_by", ""),
            "latency_ms": latency_ms,
            "missing_assets": missing if isinstance(missing, list) else [],
            "note": "；".join(note_parts)}


@app.post("/api/series/{s}/characters/{c}/portrait")
def series_character_portrait(s: str, c: str, req: dict | None = None) -> dict:
    """角色立绘：用 appearance 拼六段式走现有生图通道（size 1K），存 characters/{cid}_{ts}.png 并 append images。"""
    _series, _planner, _chars = _require_series_mods()
    if _M1_generate_image is None:
        raise HTTPException(status_code=503, detail="生图通道不可用（agnes_image 缺失）")
    try:
        _series.get_series(s)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        items = _chars.load_characters(s)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取角色失败：{exc}") from exc
    idx = -1
    for i, ch in enumerate(items):
        if isinstance(ch, dict) and str(ch.get("id", "")) == c:
            idx = i
            break
    if idx < 0:
        raise HTTPException(status_code=404, detail=f"角色 {c!r} 不存在")
    character = items[idx]
    body = req if isinstance(req, dict) else {}
    style = str(body.get("style", "") or "").strip()
    local = _local_models()
    img_cfg = local.get("image", {}) if isinstance(local, dict) else {}
    ratio = str(img_cfg.get("ratio", "9:16") or "9:16")
    try:
        prompt = _chars.build_portrait_prompt(character, style=style, ratio=ratio)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    mgr = _pool()
    if mgr is None:
        raise HTTPException(status_code=400, detail="密钥池不可用：请先在「密钥池+设置」页录入 Key")
    try:
        entry = mgr.acquire("image")
    except Exception as exc:
        if type(exc).__name__ == "PoolExhausted":
            raise HTTPException(status_code=400, detail="图片 Key 暂无可用：请检查密钥池状态或稍后重试") from exc
        raise HTTPException(status_code=500, detail=f"取 Key 失败：{exc}") from exc
    api_key = getattr(entry, "raw_key", "") or ""
    import time as _time
    start_ts = _time.perf_counter()
    try:
        try:
            gen_fn = globals().get("_generate_image") or _M1_generate_image
            result = gen_fn(prompt, size="1K", ratio=ratio, api_key=api_key)
        except Exception as exc:
            try:
                mgr.release(entry, ok=False, err={"status": 0, "body": str(exc)[:300]},
                            cost=1, kind="image")
            except Exception:
                pass
            raise HTTPException(status_code=502, detail=f"立绘生成失败：{exc}") from exc
        try:
            mgr.release(entry, ok=True, cost=1, kind="image",
                        latency_ms=round((_time.perf_counter() - start_ts) * 1000, 1))
        except Exception:
            pass
    finally:
        # 全程 try/finally 清零：gen 抛异常也清，不留明文 Key 于内存变量
        api_key = ""
    # 落盘 characters/{cid}_{ts}.png
    from datetime import datetime as _dt
    stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
    try:
        sdir = _series.series_dir(s)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    cdir = sdir / "characters"
    cdir.mkdir(parents=True, exist_ok=True)
    dest = cdir / f"{c}_{stamp}.png"
    try:
        if isinstance(result, dict) and result.get("b64"):
            import base64 as _b64
            dest.write_bytes(_b64.b64decode(result["b64"]))
        elif isinstance(result, dict) and result.get("url"):
            import httpx as _httpx
            with _httpx.stream("GET", str(result["url"]), timeout=180.0) as resp:
                resp.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in resp.iter_bytes():
                        f.write(chunk)
        else:
            raise ValueError("生图返回无 url/b64")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"立绘落盘失败：{exc}") from exc
    rel = f"characters/{dest.name}"
    if rel not in (character.get("images", []) or []):
        character.setdefault("images", []).append(rel)
    try:
        _chars.validate_character(character)
        _chars.save_characters(s, items)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    gen_by = result.get("generated_by", "") if isinstance(result, dict) else ""
    portrait_url = _character_portrait_url(s, rel)
    return {"ok": True, "character": _present_character(s, character), "path": rel,
            "portrait_url": portrait_url,
            "prompt": prompt, "generated_by": gen_by}


@app.post("/api/series/{name}/characters/{cid}/images")
async def series_character_image_upload(name: str, cid: str,
                                        request: _M1Request) -> dict:
    """角色立绘上传：multipart 单文件 file，可选 make_main（默认 true）。

    约束（与 portrait/files/upload 路由一致）：系列/角色不存在 404；
    缺 python-multipart 照现有惯例 400 指引安装；后缀仅
    .png/.jpg/.jpeg/.webp（大小写不敏感）否则 400；大小上限读
    runner.REF_IMAGE_MAX_BYTES（与参照图上限同值，不硬编码）超限 400；
    文件名只取 basename（防穿越），落盘名固定为 {cid}_{ts}{ext}（不用原名），
    存系列 characters/ 下；make_main=true 插 images[0] 否则 append（去重）；
    不碰密钥（KEK/明文无关），错误信息不带文件路径等敏感信息。
    """
    _series, _planner, _chars = _require_series_mods()
    try:
        _series.get_series(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        items = _chars.load_characters(name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取角色失败：{exc}") from exc
    idx = -1
    for i, ch in enumerate(items):
        if isinstance(ch, dict) and str(ch.get("id", "")) == cid:
            idx = i
            break
    if idx < 0:
        raise HTTPException(status_code=404, detail=f"角色 {cid!r} 不存在")
    ctype = (request.headers.get("content-type") or "").lower()
    if "multipart" not in ctype:
        raise HTTPException(status_code=400,
                            detail="请用 multipart 上传立绘（单文件 file，可选 make_main）")
    try:
        form = await request.form()
    except AssertionError as exc:
        if "python-multipart" not in str(exc):
            raise
        raise HTTPException(
            status_code=400,
            detail="缺少 python-multipart：请 pip install python-multipart "
                   "后再用 multipart 上传立绘",
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"解析上传失败：{exc}") from exc
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(status_code=400,
                            detail="缺少 file 文件字段（multipart 单文件 file）")
    orig = str(getattr(upload, "filename", "") or "")
    base = orig.replace("\\", "/").rsplit("/", 1)[-1].strip()
    ext = Path(base).suffix.lower() if base else ""
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        raise HTTPException(status_code=400, detail="仅支持 png/jpg/jpeg/webp")
    try:
        data = await upload.read()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"读取上传失败：{exc}") from exc
    if not data:
        raise HTTPException(status_code=400, detail="上传文件为空")
    try:
        _ref_max = _runner.REF_IMAGE_MAX_BYTES  # 与 runner 参照图上限同值，不硬编码
    except Exception:
        _ref_max = 5 * 1024 * 1024
    if len(data) > int(_ref_max):
        raise HTTPException(status_code=400, detail="文件超 5MB 上限")
    # make_main 默认 true：仅显式 false 系取值视为 false
    raw_mm = form.get("make_main", True)
    if raw_mm is None or (isinstance(raw_mm, str) and not raw_mm.strip()):
        make_main = True
    elif isinstance(raw_mm, bool):
        make_main = raw_mm
    else:
        make_main = str(raw_mm).strip().lower() not in (
            "false", "0", "no", "off", "n", "f")
    safe_cid = "".join(c for c in str(cid)
                       if c.isalnum() or c in "-_").strip()
    if not safe_cid:
        raise HTTPException(status_code=400, detail="角色 id 非法")
    try:
        sdir = _series.series_dir(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    cdir = sdir / "characters"
    cdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = cdir / f"{safe_cid}_{stamp}{ext}"
    try:
        dest.write_bytes(data)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"立绘落盘失败：{exc}") from exc
    rel = f"characters/{dest.name}"
    character = items[idx]
    imgs = character.get("images")
    if not isinstance(imgs, list):
        imgs = []
        character["images"] = imgs
    imgs[:] = [s for s in (str(im or "").strip() for im in imgs)
               if s and s != rel]
    if make_main:
        imgs.insert(0, rel)
    else:
        imgs.append(rel)
    character["images"] = imgs
    try:
        _chars.validate_character(character)
        _chars.save_characters(name, items)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "character": _present_character(name, character),
            "path": rel}


def _scene_image_url(series_name: str, rel: str) -> str:
    """场景图绝对 API URL（复用系列 files 直出通道）。"""
    return _character_portrait_url(series_name, rel)


def _present_scene(series_name: str, c: dict) -> dict:
    """场景表现层：附加派生 image_url（主图），只返拷贝不改落盘。"""
    d = dict(c) if isinstance(c, dict) else {}
    rel = ""
    try:
        imgs = d.get("images", [])
        if isinstance(imgs, list):
            for im in imgs:
                s = str(im or "").strip()
                if s:
                    rel = s
                    break
    except Exception:
        rel = ""
    d["image_url"] = _scene_image_url(series_name, rel) if rel else ""
    return d


@app.get("/api/series/{name}/scenes")
def series_scenes_list(name: str) -> dict:
    """场景库列表（含派生 image_url）。"""
    _series, _planner, _chars = _require_series_mods()
    mod = _scenes_mod()
    if mod is None:
        raise HTTPException(status_code=503, detail="场景模块缺失（backend/scenes.py）")
    try:
        _series.get_series(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        items = mod.load_scenes(name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取场景失败：{exc}") from exc
    return {"ok": True, "scenes": [_present_scene(name, c) for c in items]}


@app.post("/api/series/{name}/scenes")
def series_scene_create(name: str, req: dict | None = None) -> dict:
    """新建场景：{name 必填， description/first_seen/notes 可选}。"""
    _series, _planner, _chars = _require_series_mods()
    mod = _scenes_mod()
    if mod is None:
        raise HTTPException(status_code=503, detail="场景模块缺失（backend/scenes.py）")
    try:
        _series.get_series(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    body = req if isinstance(req, dict) else {}
    sname = str(body.get("name", "") or "").strip()
    if not sname:
        raise HTTPException(status_code=400, detail="场景 name 不能为空")
    try:
        items = mod.load_scenes(name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取场景失败：{exc}") from exc
    try:
        norm = mod.normalize_scene({
            "id": mod.new_scene_id(items),
            "name": sname,
            "description": str(body.get("description", "") or ""),
            "images": [],
            "first_seen": str(body.get("first_seen", "") or ""),
            "notes": str(body.get("notes", "") or ""),
            "locked": False,
        })
        mod.validate_scene(norm)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    merged = mod.merge_scenes(items, [norm])
    # 同名命中视为已存在：merge 不会新增，提示用户改名
    if len(merged) == len([c for c in items if isinstance(c, dict)]):
        raise HTTPException(status_code=400, detail=f"场景 {sname!r} 已存在")
    try:
        mod.save_scenes(name, merged)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    created = next((c for c in merged
                    if str(c.get("name", "")) == sname), norm)
    return {"ok": True, "scene": _present_scene(name, created)}


@app.patch("/api/series/{name}/scenes/{sid}")
def series_scene_patch(name: str, sid: str, req: dict | None = None) -> dict:
    """更新场景：白名单 name/description/first_seen/notes/locked。"""
    _series, _planner, _chars = _require_series_mods()
    mod = _scenes_mod()
    if mod is None:
        raise HTTPException(status_code=503, detail="场景模块缺失（backend/scenes.py）")
    try:
        _series.get_series(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        items = mod.load_scenes(name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取场景失败：{exc}") from exc
    idx = next((i for i, c in enumerate(items)
                if isinstance(c, dict) and str(c.get("id", "")) == sid), -1)
    if idx < 0:
        raise HTTPException(status_code=404, detail=f"场景 {sid!r} 不存在")
    body = req if isinstance(req, dict) else {}
    cur = dict(items[idx])
    for k in ("name", "description", "first_seen", "notes"):
        if k in body and body[k] is not None:
            v = str(body[k] or "").strip()
            if k == "name" and not v:
                raise HTTPException(status_code=400, detail="场景 name 不能为空")
            cur[k] = v
    if "images" in body and body["images"] is not None:
        raw_imgs = body["images"]
        if not isinstance(raw_imgs, list):
            raise HTTPException(status_code=400, detail="images 须为数组")
        cur["images"] = [str(x or "").strip() for x in raw_imgs
                         if str(x or "").strip()]
    if "locked" in body and body["locked"] is not None:
        v = body["locked"]
        cur["locked"] = v if isinstance(v, bool) else str(v).strip().lower() in (
            "1", "true")
    try:
        norm = mod.normalize_scene(cur)
        mod.validate_scene(norm)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    items[idx] = norm
    try:
        mod.save_scenes(name, items)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "scene": _present_scene(name, norm)}


@app.delete("/api/series/{name}/scenes/{sid}")
def series_scene_delete(name: str, sid: str) -> dict:
    """删除场景（只删库记录，不删分镜引用；渲染缺图会提示补）。"""
    _series, _planner, _chars = _require_series_mods()
    mod = _scenes_mod()
    if mod is None:
        raise HTTPException(status_code=503, detail="场景模块缺失（backend/scenes.py）")
    try:
        _series.get_series(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        items = mod.load_scenes(name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取场景失败：{exc}") from exc
    nxt = [c for c in items
           if not (isinstance(c, dict) and str(c.get("id", "")) == sid)]
    if len(nxt) == len(items):
        raise HTTPException(status_code=404, detail=f"场景 {sid!r} 不存在")
    try:
        mod.save_scenes(name, nxt)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@app.post("/api/series/{s}/scenes/{sid}/image")
def series_scene_image(s: str, sid: str, req: dict | None = None) -> dict:
    """场景图 AI 生成：用 description 拼六段式走生图通道（size 1K），存 scenes/{sid}_{ts}.png。"""
    _series, _planner, _chars = _require_series_mods()
    mod = _scenes_mod()
    if mod is None:
        raise HTTPException(status_code=503, detail="场景模块缺失（backend/scenes.py）")
    if _M1_generate_image is None:
        raise HTTPException(status_code=503, detail="生图通道不可用（agnes_image 缺失）")
    try:
        _series.get_series(s)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        items = mod.load_scenes(s)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取场景失败：{exc}") from exc
    idx = next((i for i, c in enumerate(items)
                if isinstance(c, dict) and str(c.get("id", "")) == sid), -1)
    if idx < 0:
        raise HTTPException(status_code=404, detail=f"场景 {sid!r} 不存在")
    scene = items[idx]
    body = req if isinstance(req, dict) else {}
    style = str(body.get("style", "") or "").strip()
    local = _local_models()
    img_cfg = local.get("image", {}) if isinstance(local, dict) else {}
    ratio = str(img_cfg.get("ratio", "9:16") or "9:16")
    try:
        prompt = mod.build_scene_prompt(scene, style=style, ratio=ratio)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    mgr = _pool()
    if mgr is None:
        raise HTTPException(status_code=400, detail="密钥池不可用：请先在「密钥池+设置」页录入 Key")
    try:
        entry = mgr.acquire("image")
    except Exception as exc:
        if type(exc).__name__ == "PoolExhausted":
            raise HTTPException(status_code=400, detail="图片 Key 暂无可用：请检查密钥池状态或稍后重试") from exc
        raise HTTPException(status_code=500, detail=f"取 Key 失败：{exc}") from exc
    api_key = getattr(entry, "raw_key", "") or ""
    import time as _time
    start_ts = _time.perf_counter()
    try:
        try:
            gen_fn = globals().get("_generate_image") or _M1_generate_image
            result = gen_fn(prompt, size="1K", ratio=ratio, api_key=api_key)
        except Exception as exc:
            try:
                mgr.release(entry, ok=False, err={"status": 0, "body": str(exc)[:300]},
                            cost=1, kind="image")
            except Exception:
                pass
            raise HTTPException(status_code=502, detail=f"场景图生成失败：{exc}") from exc
        try:
            mgr.release(entry, ok=True, cost=1, kind="image",
                        latency_ms=round((_time.perf_counter() - start_ts) * 1000, 1))
        except Exception:
            pass
    finally:
        api_key = ""
    from datetime import datetime as _dt
    stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
    try:
        sdir = _series.series_dir(s)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    scdir = sdir / "scenes"
    scdir.mkdir(parents=True, exist_ok=True)
    dest = scdir / f"{sid}_{stamp}.png"
    try:
        if isinstance(result, dict) and result.get("b64"):
            import base64 as _b64
            dest.write_bytes(_b64.b64decode(result["b64"]))
        elif isinstance(result, dict) and result.get("url"):
            import httpx as _httpx
            with _httpx.stream("GET", str(result["url"]), timeout=180.0) as resp:
                resp.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in resp.iter_bytes():
                        f.write(chunk)
        else:
            raise ValueError("生图返回无 url/b64")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"场景图落盘失败：{exc}") from exc
    rel = f"scenes/{dest.name}"
    if rel not in (scene.get("images", []) or []):
        scene.setdefault("images", []).append(rel)
    try:
        mod.validate_scene(scene)
        mod.save_scenes(s, items)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    gen_by = result.get("generated_by", "") if isinstance(result, dict) else ""
    return {"ok": True, "scene": _present_scene(s, scene), "path": rel,
            "image_url": _scene_image_url(s, rel),
            "prompt": prompt, "generated_by": gen_by}


@app.post("/api/series/{name}/scenes/{sid}/images")
async def series_scene_image_upload(name: str, sid: str,
                                    request: _M1Request) -> dict:
    """场景图上传：multipart 单文件 file，可选 make_main（默认 true）。

    约束与角色立绘上传一致：后缀仅 .png/.jpg/.jpeg/.webp；大小上限读
    runner.REF_IMAGE_MAX_BYTES；落盘名固定 {sid}_{ts}{ext} 存系列 scenes/ 下。
    """
    _series, _planner, _chars = _require_series_mods()
    mod = _scenes_mod()
    if mod is None:
        raise HTTPException(status_code=503, detail="场景模块缺失（backend/scenes.py）")
    try:
        _series.get_series(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        items = mod.load_scenes(name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取场景失败：{exc}") from exc
    idx = next((i for i, c in enumerate(items)
                if isinstance(c, dict) and str(c.get("id", "")) == sid), -1)
    if idx < 0:
        raise HTTPException(status_code=404, detail=f"场景 {sid!r} 不存在")
    ctype = (request.headers.get("content-type") or "").lower()
    if "multipart" not in ctype:
        raise HTTPException(status_code=400,
                            detail="请用 multipart 上传场景图（单文件 file，可选 make_main）")
    try:
        form = await request.form()
    except AssertionError as exc:
        if "python-multipart" not in str(exc):
            raise
        raise HTTPException(
            status_code=400,
            detail="缺少 python-multipart：请 pip install python-multipart "
                   "后再用 multipart 上传场景图",
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"解析上传失败：{exc}") from exc
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(status_code=400,
                            detail="缺少 file 文件字段（multipart 单文件 file）")
    orig = str(getattr(upload, "filename", "") or "")
    base = orig.replace("\\", "/").rsplit("/", 1)[-1].strip()
    ext = Path(base).suffix.lower() if base else ""
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        raise HTTPException(status_code=400, detail="仅支持 png/jpg/jpeg/webp")
    try:
        data = await upload.read()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"读取上传失败：{exc}") from exc
    if not data:
        raise HTTPException(status_code=400, detail="上传文件为空")
    try:
        _ref_max = _runner.REF_IMAGE_MAX_BYTES
    except Exception:
        _ref_max = 5 * 1024 * 1024
    if len(data) > int(_ref_max):
        raise HTTPException(status_code=400, detail="文件超 5MB 上限")
    raw_mm = form.get("make_main", True)
    if raw_mm is None or (isinstance(raw_mm, str) and not raw_mm.strip()):
        make_main = True
    elif isinstance(raw_mm, bool):
        make_main = raw_mm
    else:
        make_main = str(raw_mm).strip().lower() not in (
            "false", "0", "no", "off", "n", "f")
    safe_sid = "".join(c for c in str(sid)
                       if c.isalnum() or c in "-_").strip()
    if not safe_sid:
        raise HTTPException(status_code=400, detail="场景 id 非法")
    try:
        sdir = _series.series_dir(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    scdir = sdir / "scenes"
    scdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = scdir / f"{safe_sid}_{stamp}{ext}"
    try:
        dest.write_bytes(data)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"场景图落盘失败：{exc}") from exc
    rel = f"scenes/{dest.name}"
    scene = items[idx]
    imgs = scene.get("images")
    if not isinstance(imgs, list):
        imgs = []
        scene["images"] = imgs
    imgs[:] = [s for s in (str(im or "").strip() for im in imgs)
               if s and s != rel]
    if make_main:
        imgs.insert(0, rel)
    else:
        imgs.append(rel)
    scene["images"] = imgs
    try:
        mod.validate_scene(scene)
        mod.save_scenes(name, items)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "scene": _present_scene(name, scene),
            "path": rel}


@app.get("/api/series/{name}/files")
def series_files(name: str, path: str = "") -> Any:
    """系列文件直出（立绘 <img> 用）：仅允许系列目录内的 .png/.jpg/.jpeg/.webp。

    resolve 越界/不存在/后缀非法一律 404；media_type 按后缀。
    """
    _series, _planner, _chars = _require_series_mods()
    rel = str(path or "").strip()
    if not rel:
        raise HTTPException(status_code=404, detail="文件不存在")
    suffix = Path(rel).suffix.lower()
    media = {".png": "image/png", ".jpg": "image/jpeg",
             ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(suffix)
    if not media:
        raise HTTPException(status_code=404, detail="仅支持 png/jpg/jpeg/webp")
    try:
        sdir = _series.series_dir(name)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        target = (sdir / rel).resolve()
        root = sdir.resolve()
        target.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(status_code=404, detail="路径越界") from None
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(str(target), media_type=media)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
