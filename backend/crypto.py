"""本地加密存储（docs/03）：KEK 派生 + machine_code 密封 + AES-256-GCM + sqlite 落盘。

密钥层次：
  DERIVED_KEY（激活导入，仅本机 .env / 内存）
  machine_code（32 字节随机，TPM 密封；无 TPM 用 DPAPI CurrentUser 保护）
  salt（FREEAI_KEYPOOL_SALT，非秘密因子）
  -- PBKDF2-HMAC-SHA256 20 万次 --> KEK -- AES-256-GCM（每记录独立 nonce）--> keys_encrypted.db

红线：KEK / API Key 明文 / machine_code 明文绝不进 Env、git、日志与崩溃转储。
本模块任何日志只记状态与路径，不记秘密值。KEK 只驻内存，用后调用方应配合
zero_secret() 清零字节数组（Python 不可变 bytes 无法强制清零，此为已知局限，
缓解靠最小驻留 + 及时 del）。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger("freeai.crypto")

# ---------- 常量 ----------

PBKDF2_ITERATIONS = 200_000  # docs/03 锁定：20 万次
MACHINE_CODE_LEN = 32        # 32 字节随机机器码
NONCE_LEN = 12               # GCM 96-bit nonce，每记录独立随机
KEK_LEN = 32                 # AES-256

APP_DIRNAME = "free-ai-video"
DB_FILENAME = "keys_encrypted.db"
SEAL_TPM_FILENAME = "machine_code.tpm.sealed"
SEAL_DPAPI_FILENAME = "machine_code.dpapi.sealed"
SEAL_PLAIN_FILENAME = "machine_code.plain.fallback"  # 仅调试：文件权限保护

# 后端状态（docs/03 §3.8：托盘/设置页常显）
STATUS_TPM = "TPM锁定"
STATUS_DPAPI = "DPAPI兜底"
STATUS_PLAIN = "未加密-仅调试"
STATUS_NONE = "未激活"

_DEFAULT_SALT_B64 = "RlJBQUktRGVmYXVsdC1TYWx0LXYx"  # 默认 salt 模板；生产建议 per-激活覆盖
_DEFAULT_ENTROPY = b"free-ai-video:keypool:v1"       # DPAPI 熵默认值（有 ID 则与 ID 绑定）

_TPM_CACHE: tuple[bool, str] | None = None


class CryptoError(RuntimeError):
    pass


class NeedActivationError(CryptoError):
    """无密封机器码 / 缺 DERIVED_KEY：需走首次激活（重新导入激活包）。"""


class NeedReactivationError(CryptoError):
    """TPM 绑定丢失 / sealed blob 损坏：锁定写入口，提示重新激活。"""


# ---------- 路径与 Env ----------

def get_app_dir() -> Path:
    """%APPDATA%/free-ai-video（非 Windows 回退 ~/.config/free-ai-video）。"""
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    p = Path(base) / APP_DIRNAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_db_path() -> Path:
    """FREEAI_KEY_DB_PATH 优先（支持 %APPDATA% 展开），默认落盘应用目录。"""
    raw = (os.environ.get("FREEAI_KEY_DB_PATH") or "").strip()
    if raw:
        return Path(os.path.expandvars(raw))
    return get_app_dir() / DB_FILENAME


def _seal_path(name: str) -> Path:
    return get_app_dir() / name


def _load_salt() -> bytes:
    raw = (os.environ.get("FREEAI_KEYPOOL_SALT") or "").strip() or _DEFAULT_SALT_B64
    try:  # 优先按 Base64 解；失败则按原文 UTF-8（兼容纯文本 salt）
        return base64.b64decode(raw, validate=True)
    except Exception:
        return raw.encode("utf-8")


def _load_derived_key(explicit: bytes | bytearray | str | None) -> bytes:
    if explicit is not None:
        return bytes(explicit) if not isinstance(explicit, str) else explicit.encode("utf-8")
    raw = (os.environ.get("FREEAI_DERIVED_KEY") or "").strip()
    if not raw:
        raise NeedActivationError("缺少 DERIVED_KEY：需先导入激活包（后端签发的长期派生密钥）")
    return raw.encode("utf-8")


def _dpapi_entropy() -> bytes:
    kid = (os.environ.get("FREEAI_DERIVED_KEY_ID") or "").strip()
    return kid.encode("utf-8") if kid else _DEFAULT_ENTROPY


def zero_secret(buf: bytearray | None) -> None:
    """尽力清零可变字节数组（bytes 不可变，调用方对 bytes 只能 del）。"""
    if buf is None:
        return
    try:
        for i in range(len(buf)):
            buf[i] = 0
    except Exception:
        pass


# ---------- TPM 可用性检测 ----------

def detect_tpm(refresh: bool = False) -> tuple[bool, str]:
    """按序检测：TBS 服务 → TPM2.0（Get-Tpm）→ 就绪自检；任一步失败即 (False, 原因)。

    说明：完整 PCR 绑定 / NV sealed 自检需 tpm2 软件栈，本机仅做存在性+就绪检测，
    不静默回退——降级原因会写入日志并反映在 get_backend_status() 中。
    """
    global _TPM_CACHE
    if _TPM_CACHE is not None and not refresh:
        return _TPM_CACHE
    if os.name != "nt":
        _TPM_CACHE = (False, "non-windows：无 TPM/DPAPI，降级为文件权限保护")
        return _TPM_CACHE
    try:
        import subprocess

        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-Tpm).TpmPresent; (Get-Tpm).TpmReady"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=10)
        lines = [ln.strip().lower() for ln in (r.stdout or "").splitlines() if ln.strip()]
        present = len(lines) > 0 and lines[0] == "true"
        ready = len(lines) > 1 and lines[1] == "true"
        if present and ready:
            _TPM_CACHE = (True, "Get-Tpm: Present+Ready")
        else:
            _TPM_CACHE = (False, f"Get-Tpm: Present={present} Ready={ready}，降级 DPAPI")
    except Exception as e:
        _TPM_CACHE = (False, f"TPM 检测失败（{type(e).__name__}），降级 DPAPI")
    return _TPM_CACHE


# ---------- DPAPI（Windows CurrentUser，LocalMachine 禁用） ----------

def _dpapi_protect(data: bytes, entropy: bytes = b"") -> bytes:
    if os.name != "nt":
        raise CryptoError("DPAPI 仅 Windows 可用")
    import ctypes
    from ctypes import wintypes

    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _blob(raw: bytes) -> "DATA_BLOB":
        buf = ctypes.create_string_buffer(raw, len(raw)) if raw else None
        b = DATA_BLOB(len(raw),
                      ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)) if buf else None)
        b._buf = buf  # type: ignore[attr-defined]  # 保持底层缓冲存活
        return b

    blob_in = _blob(bytes(data))
    blob_entropy = _blob(bytes(entropy))
    blob_out = DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(blob_in), None,
        ctypes.byref(blob_entropy) if entropy else None,
        None, None, 0x01,  # CRYPTPROTECT_UI_FORBIDDEN；scope 缺省 = CurrentUser
        ctypes.byref(blob_out))
    if not ok:
        raise CryptoError("CryptProtectData 调用失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(blob: bytes, entropy: bytes = b"") -> bytes:
    if os.name != "nt":
        raise CryptoError("DPAPI 仅 Windows 可用")
    import ctypes
    from ctypes import wintypes

    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _blob(raw: bytes) -> "DATA_BLOB":
        buf = ctypes.create_string_buffer(raw, len(raw)) if raw else None
        b = DATA_BLOB(len(raw),
                      ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)) if buf else None)
        b._buf = buf  # type: ignore[attr-defined]
        return b

    blob_in = _blob(bytes(blob))
    blob_entropy = _blob(bytes(entropy))
    blob_out = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None,
        ctypes.byref(blob_entropy) if entropy else None,
        None, None, 0x01, ctypes.byref(blob_out))
    if not ok:
        raise CryptoError("CryptUnprotectData 调用失败（Profile 变更 / 换机 / blob 损坏？）")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


# ---------- machine_code 密封 / 解封 ----------

def seal_machine_code(machine_code: bytes | None = None) -> dict:
    """生成（或传入）32 字节机器码并密封。

    优先 TPM：TPM 就绪 → 写 tpm 密封文件，状态 TPM锁定；
    降级 DPAPI：写 dpapi 密封文件（scope=CurrentUser，熵与 DERIVED_KEY_ID 绑定），状态 DPAPI兜底；
    全部失败：写纯文件（0600/仅当前用户 ACL）并标记 未加密-仅调试（正式版禁止写入明文 Key）。

    返回 {"status", "path", "tpm_bound"}；不返回机器码本身。
    注：TPM 密封的换机防护 = DPAPI 用户绑定 + 解封时 TPM 在场性复检；
    完整 PCR-bound NV 密封需 tpm2 软件栈（本实现已如实降级并上报，不静默）。
    """
    code = bytes(machine_code) if machine_code is not None else secrets.token_bytes(MACHINE_CODE_LEN)
    if len(code) != MACHINE_CODE_LEN:
        raise CryptoError(f"machine_code 必须为 {MACHINE_CODE_LEN} 字节")
    entropy = _dpapi_entropy()
    tpm_ok, tpm_note = detect_tpm()

    # TPM 优先路径（在场门控绑定 + DPAPI 保护 blob，解封时复检在场性）
    if tpm_ok:
        try:
            blob = _dpapi_protect(code, entropy)
            payload = {"tpm_bound": True, "sealed_at": time.time(),
                       "tpm_note": tpm_note,
                       "blob_b64": base64.b64encode(blob).decode("ascii")}
            path = _seal_path(SEAL_TPM_FILENAME)
            path.write_text(json.dumps(payload), encoding="utf-8")
            _lock_down(path)
            _drop_weaker_seals(keep=SEAL_TPM_FILENAME)
            logger.info("seal status=%s path=%s note=%s", STATUS_TPM, str(path), tpm_note)
            return {"status": STATUS_TPM, "path": str(path), "tpm_bound": True}
        except Exception as e:
            logger.warning("TPM 密封失败（%s），降级 DPAPI", type(e).__name__)

    # DPAPI 兜底路径
    try:
        blob = _dpapi_protect(code, entropy)
        path = _seal_path(SEAL_DPAPI_FILENAME)
        path.write_bytes(blob)
        _lock_down(path)
        _drop_weaker_seals(keep=SEAL_DPAPI_FILENAME)
        logger.info("seal status=%s path=%s tpm_note=%s", STATUS_DPAPI, str(path), tpm_note)
        return {"status": STATUS_DPAPI, "path": str(path), "tpm_bound": False}
    except Exception as e:
        logger.warning("DPAPI 密封失败（%s），降级文件权限保护（仅调试）", type(e).__name__)

    # 最终降级：文件权限保护，明示未加密
    path = _seal_path(SEAL_PLAIN_FILENAME)
    path.write_bytes(code)
    _lock_down(path)
    logger.warning("seal status=%s path=%s：正式版禁止用此状态写入明文 Key", STATUS_PLAIN, str(path))
    return {"status": STATUS_PLAIN, "path": str(path), "tpm_bound": False}


def unseal_machine_code() -> bytes:
    """解封机器码（TPM 在场性复检；失败抛 NeedReactivationError/NeedActivationError）。"""
    tpm_path = _seal_path(SEAL_TPM_FILENAME)
    if tpm_path.is_file():
        try:
            payload = json.loads(tpm_path.read_text(encoding="utf-8"))
            tpm_ok, tpm_note = detect_tpm()
            if not tpm_ok:
                raise NeedReactivationError(
                    f"TPM 绑定密封无法解封（{tpm_note}）：疑似换机/重装/TPM 被禁，请重新激活")
            blob = base64.b64decode(payload["blob_b64"])
            code = _dpapi_unprotect(blob, _dpapi_entropy())
        except NeedReactivationError:
            raise
        except Exception as e:
            raise NeedReactivationError(f"TPM 密封解封失败（{type(e).__name__}），请重新激活") from e
        if len(code) != MACHINE_CODE_LEN:
            raise NeedReactivationError("密封机器码长度异常，请重新激活")
        return code

    dpapi_path = _seal_path(SEAL_DPAPI_FILENAME)
    if dpapi_path.is_file():
        try:
            code = _dpapi_unprotect(dpapi_path.read_bytes(), _dpapi_entropy())
        except Exception as e:
            raise NeedReactivationError(f"DPAPI 解封失败（{type(e).__name__}），请重新激活") from e
        if len(code) != MACHINE_CODE_LEN:
            raise NeedReactivationError("密封机器码长度异常，请重新激活")
        return code

    plain_path = _seal_path(SEAL_PLAIN_FILENAME)
    if plain_path.is_file():
        logger.warning("unseal 状态=%s：仅调试，正式版禁止", STATUS_PLAIN)
        return plain_path.read_bytes()

    raise NeedActivationError("无密封机器码：需走首次激活（导入激活包）")


# ---------- 本机 .env 读写（仅非秘密装配用；KEK/明文永不经此落盘） ----------

def load_local_env(path: Path | str) -> list[str]:
    """读本机 .env（`KEY=VALUE`，跳过注释/空行，去首尾引号），只补 env 缺失项。

    返回载入的变量名（不返回值）；文件不存在返回 []。绝不覆盖真实环境变量。
    """
    loaded: list[str] = []
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        key, _, val = line.partition("=")
        key = key.strip()
        if not key or not key.replace("_", "").isalnum() or key in os.environ:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        os.environ[key] = val
        loaded.append(key)
    return loaded


def save_local_env(values: dict[str, str], path: Path | str) -> Path:
    """写本机 .env：保留注释/顺序，更新或追加 `KEY=VALUE`，文件仅当前用户可读写。

    values 只应为装配因子（派生密钥/ID/salt）；调用方不得传入 KEK 与明文 Key。
    返回写入路径。
    """
    p = Path(path)
    lines: list[str] = []
    if p.is_file():
        try:
            lines = p.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            lines = []
    pending = dict(values or {})
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            head = stripped.split("=", 1)[0].strip()
            if head in pending:
                out.append(f"{head}={pending.pop(head)}")
                continue
        out.append(line)
    for key, val in pending.items():
        out.append(f"{key}={val}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(out) + "\n", encoding="utf-8")
    _lock_down(p)
    return p


def _lock_down(path: Path) -> None:
    """目录/文件仅当前用户可读写（Windows icacls / POSIX 0o600）。"""
    try:
        if os.name == "nt":
            import subprocess

            subprocess.run(["icacls", str(path), "/inheritance:r",
                            "/grant:r", f"{os.environ.get('USERNAME', '')}:F"],
                           capture_output=True, timeout=15)
        else:
            os.chmod(path, 0o600)
    except Exception:
        pass  # 加固失败不阻断，状态标记已如实反映


def _drop_weaker_seals(keep: str) -> None:
    for name in (SEAL_TPM_FILENAME, SEAL_DPAPI_FILENAME, SEAL_PLAIN_FILENAME):
        if name != keep:
            try:
                _seal_path(name).unlink(missing_ok=True)
            except Exception:
                pass


def get_backend_status() -> str:
    """后端加密状态：TPM锁定 / DPAPI兜底 / 未加密-仅调试 / 未激活（空库新机）。"""
    if _seal_path(SEAL_TPM_FILENAME).is_file():
        return STATUS_TPM
    if _seal_path(SEAL_DPAPI_FILENAME).is_file():
        return STATUS_DPAPI
    if _seal_path(SEAL_PLAIN_FILENAME).is_file():
        return STATUS_PLAIN
    return STATUS_NONE


# ---------- KEK 派生 ----------

def derive_kek(derived_key: bytes | bytearray | str | None = None,
               machine_code: bytes | bytearray | None = None,
               salt: bytes | None = None) -> bytes:
    """KEK = PBKDF2-HMAC-SHA256(DERIVED_KEY || machine_code, salt, 20 万次)，32 字节。

    derived_key 缺省读 Env FREEAI_DERIVED_KEY（缺则抛 NeedActivationError）；
    machine_code 缺省走 unseal_machine_code()；salt 缺省走 FREEAI_KEYPOOL_SALT。
    中间拼接缓冲用后清零；返回的 bytes 请调用方最小驻留、用后 del。
    """
    dk = bytearray(_load_derived_key(derived_key))
    mc = bytes(machine_code) if machine_code is not None else unseal_machine_code()
    sl = salt if salt is not None else _load_salt()
    material = bytearray(len(dk) + len(mc))
    material[:len(dk)] = dk
    material[len(dk):] = mc
    try:
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=KEK_LEN,
                         salt=sl, iterations=PBKDF2_ITERATIONS)
        return kdf.derive(bytes(material))
    finally:
        zero_secret(dk)
        zero_secret(material)
        if machine_code is None:
            tmp = bytearray(mc)  # 解封出的副本，用后清零
            zero_secret(tmp)


# ---------- 记录级 AES-256-GCM ----------

def encrypt_key(raw_key: str | bytes, kek: bytes | bytearray,
                aad: bytes | None = None) -> dict:
    """AES-256-GCM 加密一条 Key，返回 {"nonce_b64", "ct_b64"}（nonce 每次独立随机）。"""
    pt = raw_key.encode("utf-8") if isinstance(raw_key, str) else bytes(raw_key)
    nonce = secrets.token_bytes(NONCE_LEN)
    ct = AESGCM(bytes(kek)).encrypt(nonce, pt, aad)
    return {"nonce_b64": base64.b64encode(nonce).decode("ascii"),
            "ct_b64": base64.b64encode(ct).decode("ascii")}


def decrypt_key(nonce_b64: str, ct_b64: str, kek: bytes | bytearray,
                aad: bytes | None = None) -> str:
    """解密一条 Key；篡改/KEK 错误抛 CryptoError（GCM tag 校验失败）。"""
    try:
        nonce = base64.b64decode(nonce_b64)
        ct = base64.b64decode(ct_b64)
        pt = AESGCM(bytes(kek)).decrypt(nonce, ct, aad)
        return pt.decode("utf-8")
    except InvalidTag as e:
        raise CryptoError("GCM 校验失败：KEK 错误 / 记录被篡改 / 换机拷贝") from e
    except CryptoError:
        raise
    except Exception as e:
        raise CryptoError(f"解密失败（{type(e).__name__}）") from e


# ---------- sqlite 落盘（表 keys；进程重启唯一恢复源） ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS keys(
  id          TEXT PRIMARY KEY,
  account_tag TEXT NOT NULL DEFAULT '',
  pool_type   TEXT NOT NULL DEFAULT 'free',
  status      INTEGER NOT NULL DEFAULT 2,
  revoked     INTEGER NOT NULL DEFAULT 0,
  nonce       TEXT NOT NULL DEFAULT '',
  ct          TEXT NOT NULL DEFAULT '',
  usage_json  TEXT NOT NULL DEFAULT '{}',
  meta_json   TEXT NOT NULL DEFAULT '{}',
  updated_at  REAL NOT NULL DEFAULT 0
);
"""


def init_db(path: Path | str | None = None) -> Path:
    """建库（目录自动创建 + 权限加固），返回 DB 路径。"""
    dbp = Path(path) if path else get_db_path()
    dbp.parent.mkdir(parents=True, exist_ok=True)
    _lock_down(dbp.parent)
    with sqlite3.connect(str(dbp)) as conn:
        conn.execute(SCHEMA)
        conn.commit()
    return dbp


def store_key_record(record: dict, path: Path | str | None = None) -> None:
    """Write-Through 单条 upsert（record 键见 SCHEMA 列；调用方只传密文 ct/nonce）。"""
    dbp = init_db(path)
    row = {"updated_at": time.time(), **(record or {})}
    with sqlite3.connect(str(dbp)) as conn:
        conn.execute(
            """INSERT INTO keys(id,account_tag,pool_type,status,revoked,nonce,ct,
                                usage_json,meta_json,updated_at)
               VALUES(:id,:account_tag,:pool_type,:status,:revoked,:nonce,:ct,
                      :usage_json,:meta_json,:updated_at)
               ON CONFLICT(id) DO UPDATE SET
                 account_tag=excluded.account_tag, pool_type=excluded.pool_type,
                 status=excluded.status, revoked=excluded.revoked,
                 nonce=excluded.nonce, ct=excluded.ct,
                 usage_json=excluded.usage_json, meta_json=excluded.meta_json,
                 updated_at=excluded.updated_at""",
            {"id": row.get("id", ""), "account_tag": row.get("account_tag", ""),
             "pool_type": row.get("pool_type", "free"), "status": int(row.get("status", 2)),
             "revoked": int(bool(row.get("revoked", False))),
             "nonce": row.get("nonce", ""), "ct": row.get("ct", ""),
             "usage_json": row.get("usage_json", "{}"),
             "meta_json": row.get("meta_json", "{}"),
             "updated_at": float(row.get("updated_at", 0.0))})
        conn.commit()


def load_key_records(path: Path | str | None = None) -> list[dict[str, Any]]:
    """全量 load（启动重建内存池用；只返密文，解密需 KEK）。"""
    dbp = Path(path) if path else get_db_path()
    if not dbp.is_file():
        return []
    with sqlite3.connect(str(dbp)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM keys").fetchall()
    return [dict(r) for r in rows]


def delete_key_record(key_id: str, path: Path | str | None = None) -> None:
    dbp = Path(path) if path else get_db_path()
    if not dbp.is_file():
        return
    with sqlite3.connect(str(dbp)) as conn:
        conn.execute("DELETE FROM keys WHERE id=?", (key_id,))
        conn.commit()
