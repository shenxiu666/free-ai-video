"""三层配置合并：CLI > ENV > config/models.yaml（docs/01 ADR-3、docs/05）。

ENV 映射：
  AGNES_API_KEY / AGNES_API_KEYS      -> auth.agnes_api_keys（逗号分隔，多账号多 Key）
  AGNES_TEXT_MODEL                     -> text.model（带 provider/ 前缀时同步推断 provider）
  AGNES_IMAGE_MODEL / AGNES_VIDEO_MODEL -> image.model / video.model
  TTS_PROVIDER / TTS_VOICE             -> tts.provider / tts.voice
  FREEAI_TEXT_PROVIDER / FREEAI_TEXT_MODEL / FREEAI_FALLBACK_MODE -> text.*
  FREEAI_TEXT_PROTOCOL / FREEAI_TEXT_BASE_URL / FREEAI_TEXT_TEMPERATURE
  FREEAI_TEXT_MAX_TOKENS / FREEAI_TEXT_TIMEOUT_S -> text.* 高级参数
  FREEAI_IMAGE_MODEL / FREEAI_VIDEO_MODEL                          -> image/video.model
  FREEAI_TTS_PROVIDER / FREEAI_TTS_VOICE                           -> tts.*
  FREEAI_KEY_VERSION / FREEAI_KEYPOOL_SALT / FREEAI_DERIVED_KEY_ID
  FREEAI_KEY_DB_PATH                   -> keypool.*（salt/版本/引用/路径为非秘密因子，可进配置）

红线：本模块绝不打印 / 记录 KEK、API Key 明文、机器码明文；对外展示一律用
masked_config() / safe_summary()（掩码 sk-前2~~~~后3）。
"""

from __future__ import annotations

import copy
import os
import threading
from pathlib import Path

try:
    import yaml  # type: ignore

    _YAML_OK = True
except Exception:  # 缺依赖时降级为 DEFAULTS，保证可 import、可自测
    yaml = None  # type: ignore
    _YAML_OK = False

REPO_ROOT = Path(__file__).resolve().parent.parent
MODELS_YAML = REPO_ROOT / "config" / "models.yaml"

DEFAULTS: dict = {
    "text": {"provider": "", "model": "", "protocol": "openai", "base_url": "",
             "temperature": 0.7, "max_tokens": 8192, "timeout_s": 120,
             "fallback": {"mode": "manual"}},
    "image": {"provider": "agnes", "model": "agnes-image-2.5-flash",
              "size": "1K", "ratio": "9:16"},
    "video": {"provider": "agnes", "model": "agnes-video-2.5-flash",
              "size": "720P", "mode": "reference", "seconds": "8",
              "aspect_ratio": "9:16"},
    "tts": {"provider": "edge-tts", "voice": "zh-CN-XiaoxiaoNeural"},
    "auth": {"agnes_api_keys": []},
    "keypool": {"key_version": "v1", "salt": "", "derived_key_id": "",
                "db_path": ""},
}

_lock = threading.Lock()
_cache: dict | None = None


# ---------- 基础工具 ----------

def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并 override 到 base（就地），返回 base。"""
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _mask_key(raw: str) -> str:
    """掩码：sk-前2~~~~后3（与 keypool.mask_key 同规则，独立实现以避免循环导入）。"""
    s = str(raw or "")
    if not s:
        return ""
    core = s[3:] if s.startswith("sk-") else s
    return f"sk-{core[:2]}~~~~{s[-3:]}"


def _split_csv(value: str) -> list[str]:
    parts: list[str] = []
    for chunk in str(value or "").replace(";", ",").replace("\n", ",").split(","):
        chunk = chunk.strip().strip("'\"")
        if chunk:
            parts.append(chunk)
    return parts


def _load_yaml_file() -> dict:
    if not _YAML_OK:
        return {}
    try:
        if not MODELS_YAML.is_file():
            return {}
        with open(MODELS_YAML, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _infer_text_provider(model: str, current: str) -> str:
    """model 形如 agnes/... 或 opencode/... 且 provider 留空时，同步推断 provider。"""
    if current or "/" not in str(model or ""):
        return current
    head = str(model).split("/", 1)[0].strip().lower()
    if head in ("agnes", "opencode"):
        return head
    return current


# ---------- 三层合并 ----------

def _apply_env(cfg: dict) -> dict:
    env = os.environ

    # Agnes 鉴权（多 Key 逗号分隔；AGNES_API_KEYS 优先）
    multi = env.get("AGNES_API_KEYS", "") or env.get("AGNES_API_KEY", "")
    keys = _split_csv(multi)
    if keys:
        cfg.setdefault("auth", {})["agnes_api_keys"] = keys

    # Agnes 模型覆盖
    if env.get("AGNES_TEXT_MODEL"):
        cfg.setdefault("text", {})["model"] = env["AGNES_TEXT_MODEL"].strip()
        cfg["text"]["provider"] = _infer_text_provider(
            cfg["text"]["model"], cfg["text"].get("provider", ""))
    if env.get("AGNES_IMAGE_MODEL"):
        cfg.setdefault("image", {})["model"] = env["AGNES_IMAGE_MODEL"].strip()
    if env.get("AGNES_VIDEO_MODEL"):
        cfg.setdefault("video", {})["model"] = env["AGNES_VIDEO_MODEL"].strip()

    # TTS 覆盖
    if env.get("TTS_PROVIDER"):
        cfg.setdefault("tts", {})["provider"] = env["TTS_PROVIDER"].strip()
    if env.get("TTS_VOICE"):
        cfg.setdefault("tts", {})["voice"] = env["TTS_VOICE"].strip()

    # FREEAI_* 通用覆盖（与上面等价的另一套前缀，显式配置才生效）
    text = cfg.setdefault("text", {})
    if env.get("FREEAI_TEXT_PROVIDER"):
        text["provider"] = env["FREEAI_TEXT_PROVIDER"].strip()
    if env.get("FREEAI_TEXT_MODEL"):
        text["model"] = env["FREEAI_TEXT_MODEL"].strip()
        text["provider"] = _infer_text_provider(text["model"], text.get("provider", ""))
    if env.get("FREEAI_TEXT_PROTOCOL"):
        text["protocol"] = env["FREEAI_TEXT_PROTOCOL"].strip().lower()
    if env.get("FREEAI_TEXT_BASE_URL"):
        text["base_url"] = env["FREEAI_TEXT_BASE_URL"].strip()
    if env.get("FREEAI_TEXT_TEMPERATURE"):
        try:
            text["temperature"] = float(env["FREEAI_TEXT_TEMPERATURE"])
        except ValueError:
            pass
    if env.get("FREEAI_TEXT_MAX_TOKENS"):
        try:
            text["max_tokens"] = int(env["FREEAI_TEXT_MAX_TOKENS"])
        except ValueError:
            pass
    if env.get("FREEAI_TEXT_TIMEOUT_S"):
        try:
            text["timeout_s"] = float(env["FREEAI_TEXT_TIMEOUT_S"])
        except ValueError:
            pass
    if env.get("FREEAI_FALLBACK_MODE"):
        text.setdefault("fallback", {})["mode"] = env["FREEAI_FALLBACK_MODE"].strip()
    if env.get("FREEAI_IMAGE_MODEL"):
        cfg.setdefault("image", {})["model"] = env["FREEAI_IMAGE_MODEL"].strip()
    if env.get("FREEAI_VIDEO_MODEL"):
        cfg.setdefault("video", {})["model"] = env["FREEAI_VIDEO_MODEL"].strip()
    tts = cfg.setdefault("tts", {})
    if env.get("FREEAI_TTS_PROVIDER"):
        tts["provider"] = env["FREEAI_TTS_PROVIDER"].strip()
    if env.get("FREEAI_TTS_VOICE"):
        tts["voice"] = env["FREEAI_TTS_VOICE"].strip()

    # KeyPool 非秘密因子（salt/版本/引用 ID/路径；绝不含 KEK 与明文）
    kp = cfg.setdefault("keypool", {})
    if env.get("FREEAI_KEY_VERSION"):
        kp["key_version"] = env["FREEAI_KEY_VERSION"].strip()
    if env.get("FREEAI_KEYPOOL_SALT"):
        kp["salt"] = env["FREEAI_KEYPOOL_SALT"].strip()
    if env.get("FREEAI_DERIVED_KEY_ID"):
        kp["derived_key_id"] = env["FREEAI_DERIVED_KEY_ID"].strip()
    if env.get("FREEAI_KEY_DB_PATH"):
        kp["db_path"] = os.path.expandvars(env["FREEAI_KEY_DB_PATH"].strip())
    return cfg


def _normalize(cfg: dict) -> dict:
    """类型归一：video.size / video.seconds 必须为字符串（Agnes 拒收数字，见 docs/05 §5.7）；
    text.temperature / max_tokens / timeout_s 归一为数字（失败回默认值）。"""
    video = cfg.get("video")
    if isinstance(video, dict):
        if "size" in video and not isinstance(video["size"], str):
            video["size"] = str(video["size"])
        if "seconds" in video and not isinstance(video["seconds"], str):
            video["seconds"] = str(video["seconds"])
    text = cfg.get("text")
    if isinstance(text, dict):
        try:
            text["temperature"] = float(text.get("temperature", 0.7))
        except (TypeError, ValueError):
            text["temperature"] = 0.7
        try:
            text["max_tokens"] = int(text.get("max_tokens", 8192))
        except (TypeError, ValueError):
            text["max_tokens"] = 8192
        try:
            text["timeout_s"] = float(text.get("timeout_s", 120))
        except (TypeError, ValueError):
            text["timeout_s"] = 120
        if not isinstance(text.get("protocol"), str) or not text["protocol"]:
            text["protocol"] = "openai"
        if not isinstance(text.get("base_url"), str):
            text["base_url"] = ""
    return cfg


def load_models_config(cli_overrides: dict | None = None) -> dict:
    """三层合并并返回全新 dict：CLI > ENV > YAML（YAML 缺失则回退 DEFAULTS）。

    cli_overrides 为嵌套 dict，如 {"text": {"model": "opencode/mimo-v2.5-free"}}。
    本函数不缓存、不打印任何秘密值。
    """
    cfg = copy.deepcopy(DEFAULTS)
    _deep_merge(cfg, _load_yaml_file())
    _apply_env(cfg)
    if cli_overrides:
        if not isinstance(cli_overrides, dict):
            raise TypeError("cli_overrides 必须为 dict 或 None")
        _deep_merge(cfg, copy.deepcopy(cli_overrides))
    return _normalize(cfg)


def get() -> dict:
    """单例：首次调用按（ENV > YAML）合并并缓存；返回深拷贝，调用方改不动缓存。

    ENV 变更后调 reload_config() 刷新。
    """
    global _cache
    with _lock:
        if _cache is None:
            _cache = load_models_config()
        return copy.deepcopy(_cache)


def reload_config(cli_overrides: dict | None = None) -> dict:
    """强制重载并刷新单例缓存，返回深拷贝。"""
    global _cache
    with _lock:
        _cache = load_models_config(cli_overrides)
        return copy.deepcopy(_cache)


# ---------- 脱敏展示 ----------

def masked_config(cfg: dict) -> dict:
    """返回脱敏副本：agnes_api_keys 只留 sk-前2~~~~后3；其余原样。"""
    out = copy.deepcopy(cfg or {})
    try:
        keys = out.get("auth", {}).get("agnes_api_keys", [])
        out.setdefault("auth", {})["agnes_api_keys"] = [_mask_key(k) for k in keys]
    except Exception:
        pass
    return out


def safe_summary(cfg: dict | None = None) -> dict:
    """给日志 / 前端的安全摘要：脱敏后的模型配置（绝不含明文 Key）。"""
    return masked_config(cfg if cfg is not None else get())
