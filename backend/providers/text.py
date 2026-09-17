"""开放文本 Provider：无主力、用户自配（docs/05.1）。

- text.provider/model 默认留空；留空即 raise TextNotConfigured 并指引去设置页。
- fallback 默认 manual：不自动降级；仅用户显式配置 fallback 才降级。
- 双通道：agnes / openai-compatible 走 HTTPS（OpenAI 兼容接口）；
  opencode-zen（`opencode/*` 免费模型）走本地 opencode CLI
  （`run --model ... --format json`），免登录（已实测）。
- 返回必带 generated_by(provider+model+time)。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

try:
    import opencode_manager as _om  # type: ignore
except ImportError:
    try:
        import backend.opencode_manager as _om  # type: ignore
    except ImportError:
        _om = None  # type: ignore

AGNES_CHAT_URL = "https://apihub.agnes-ai.com/v1/chat/completions"

SUPPORTED_PROVIDERS = frozenset({"agnes", "opencode-zen", "zen", "openai-compatible"})

SETTINGS_HINT = "请前往「密钥池+设置」页填写文本 provider/model 后重试"


class TextNotConfigured(Exception):
    """文本模型未配置（provider/model 留空）。"""


def _normalize_provider(provider: str) -> str:
    p = (provider or "").strip().lower()
    if p in ("zen", "opencode-zen", "opencode/zen"):
        return "opencode-zen"
    return p


def _resolve(
    provider: str, model: str, base_url: str = "", api_key: str = ""
) -> tuple[str, str, str, str]:
    p = _normalize_provider(provider)
    m = (model or "").strip()
    url = (base_url or "").strip()
    # 容错：provider 误填成 URL（形如 https://.../v1）时视为 openai-compatible 端点
    if p and (p.startswith("http://") or p.startswith("https://")):
        url = url or p.rstrip("/")
        p = "openai-compatible"
    if not p or not m:
        raise TextNotConfigured(
            f"文本模型未配置（provider={provider!r}, model={model!r}）。"
            + SETTINGS_HINT
        )
    if p not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"文本 provider 不支持: {provider!r}，支持 {sorted(SUPPORTED_PROVIDERS)}"
        )
    key = api_key or ""
    url = url or (base_url or "").strip()  # 保留上方的 URL 容错推断结果
    if p == "agnes":
        url = url or AGNES_CHAT_URL
        key = key or os.environ.get("AGNES_API_KEY", "")
    elif p == "opencode-zen":
        # CLI 通道：免登录，不需要端点与 Key（见 generate_text）
        url = ""
        key = ""
    else:  # openai-compatible（自建/第三方）
        if not url:
            raise ValueError(
                "openai-compatible 需提供 base_url。" + SETTINGS_HINT
            )
    if p != "opencode-zen" and not key:
        raise TextNotConfigured(
            f"文本鉴权缺失（provider={p} 未提供 api_key）。"
            + SETTINGS_HINT
        )
    return p, m, url, key


def _chat_once(
    url: str, key: str, model: str, prompt: str,
    timeout: float, client: Optional[Any],
    temperature: float = 0.7, max_tokens: int = 4096,
    system: str = "",
) -> str:
    payload: dict[str, Any] = {
        "model": model,
        "messages": ([{"role": "system", "content": system}] if system else [])
        + [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {"Authorization": f"Bearer {key}"}
    own_client = client is None
    http = client or httpx.Client(timeout=float(timeout))
    try:
        resp = http.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    finally:
        if own_client:
            http.close()
    try:
        return str(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"文本返回无法解析: {str(data)[:300]}")


def generate_text(
    prompt: str,
    provider: str = "",
    model: str = "",
    fallback_mode: str = "manual",
    fallback: Optional[dict] = None,
    api_key: str = "",
    base_url: str = "",
    timeout: float = 60,
    client: Optional[Any] = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    system: str = "",
    bin_path: str = "",
) -> dict:
    """调用用户自配文本模型。

    fallback_mode 默认 "manual"：主键失败直接抛，不自动降级；
    仅当 fallback_mode != "manual" 且显式传入 fallback 配置时才降级一次。
    opencode-zen 走本地 CLI（免登录；temperature/max_tokens 不透传，用模型默认）。
    返回 {"text", "generated_by"}。
    """
    if not prompt or not str(prompt).strip():
        raise ValueError("文本 prompt 不能为空")
    provider_n, model_n, url, key = _resolve(provider, model, base_url, api_key)
    if provider_n == "opencode-zen":
        try:
            return _cli_text(provider_n, model_n, prompt, system, timeout, bin_path)
        except TextNotConfigured:
            raise
        except Exception as exc:
            use_fallback = (
                fallback_mode != "manual" and isinstance(fallback, dict)
                and fallback.get("provider") and fallback.get("model")
            )
            if not use_fallback:
                raise
            return _fallback_https(fallback, prompt, timeout, client,
                                   temperature, max_tokens, system,
                                   provider_n, model_n)
    try:
        text = _chat_once(url, key, model_n, prompt, timeout, client,
                           temperature, max_tokens, system)
    except TextNotConfigured:
        raise
    except Exception as exc:  # noqa: BLE001 — 降级决策需要捕获主键失败
        use_fallback = (
            fallback_mode != "manual" and isinstance(fallback, dict)
            and fallback.get("provider") and fallback.get("model")
        )
        if not use_fallback:
            raise
        return _fallback_https(fallback, prompt, timeout, client,
                               temperature, max_tokens, system,
                               provider_n, model_n)
    return {
        "text": text,
        "generated_by": (
            f"{provider_n}/{model_n}@{datetime.now(timezone.utc).isoformat()}"
        ),
    }


def _cli_text(provider_n: str, model_n: str, prompt: str, system: str,
              timeout: float, bin_path: str) -> dict:
    """opencode-zen 经本地 CLI（免登录免费通道）。"""
    if _om is None:
        raise RuntimeError("opencode 管理器缺失，CLI 通道不可用")
    full = model_n if "/" in model_n else f"opencode/{model_n}"
    out = _om.run_text(bin_path=bin_path, model=full, prompt=prompt,
                       system=system, timeout=timeout)
    return {
        "text": out["text"],
        "generated_by": (
            f"{provider_n}/{full}@{datetime.now(timezone.utc).isoformat()} (cli)"
        ),
    }


def _fallback_https(fallback: dict, prompt: str, timeout: float,
                    client: Optional[Any], temperature: float,
                    max_tokens: int, system: str,
                    provider_n: str, model_n: str) -> dict:
    fp, fm, furl, fkey = _resolve(
        str(fallback.get("provider", "")),
        str(fallback.get("model", "")),
        str(fallback.get("base_url", "")),
        str(fallback.get("api_key", "")),
    )
    if fp == "opencode-zen":
        out = _cli_text(fp, fm, prompt, system, timeout,
                        str(fallback.get("bin_path", "")))
        text = out["text"]
    else:
        text = _chat_once(furl, fkey, fm, prompt, timeout, client,
                           float(fallback.get("temperature", temperature)),
                           int(fallback.get("max_tokens", max_tokens)), system)
    return {
        "text": text,
        "generated_by": (
            f"{fp}/{fm}@{datetime.now(timezone.utc).isoformat()}"
            f" (fallback from {provider_n}/{model_n})"
        ),
    }
    return {
        "text": text,
        "generated_by": (
            f"{provider_n}/{model_n}@{datetime.now(timezone.utc).isoformat()}"
        ),
    }
