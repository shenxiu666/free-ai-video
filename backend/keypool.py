"""多 Key 调度池（docs/02）：内存 Lock 池 + 加密 DB 写透 + 前端掩码层。

三层架构：加密 DB（Single Source of Truth）→ 内存 Lock 池 → 前端掩码层。
默认多免费 Key = 不同账号 = 独立池；同 account_tag 多 Key 联动 429 时自动
归并串行（shared_group），30min 无联动自动解散。

日志字段（docs/02 §2.10）：time, key_mask, account_tag, pool_type,
event(acquire/release/cooldown/exhausted/revoked/sweep/merge), kind, cost,
retry_after, latency_ms；冷却与归并为 WARN。日志只含掩码，永不含 Key 明文。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore

    _YAML_OK = True
except Exception:
    yaml = None  # type: ignore
    _YAML_OK = False

try:
    from backend import crypto as _crypto

    _CRYPTO_OK = True
except Exception:
    try:  # 允许 workdir=backend 直接启动（顶层 crypto 模块）
        import crypto as _crypto  # type: ignore[no-redef]

        _CRYPTO_OK = True
    except Exception:  # 纯内存自测时允许无 crypto
        _crypto = None  # type: ignore
        _CRYPTO_OK = False

logger = logging.getLogger("freeai.keypool")

# ---------- 常量（docs/02 §2.4/§2.5/§2.7，均为启发式、待实采校准） ----------

KINDS = ("text", "image", "video")

POOL_FREE = "free"
POOL_ENTERPRISE = "enterprise"
POOL_TOKENPLAN = "tokenplan"

STATUS_IDLE = 2
STATUS_BUSY = 1
STATUS_DRAINED = 0

# 自建端点专用密钥的账号备注（docs/05 §5.9）：池顶 Key 只给 Agnes 用，
# 自建 OpenAI 兼容端点必须用此备注的专用密钥，二者绝不串用。
CUSTOM_ENDPOINT_TAG = "自建端点"

LEASE_SECONDS = 10 * 60       # 使用中租约 10min，防任务卡死
SWEEP_INTERVAL = 60           # 后台 sweep 周期 60s
DAY_SECONDS = 24 * 3600       # 24h 滚动窗口
SHARED_WINDOW = 60            # 共享池探测滑动窗口 60s
SHARED_DISSOLVE = 30 * 60     # 30min 无联动解散

# RPM 令牌桶（免费参考值，可能调整；TokenPlan 不限速只看日配额；企业待实采）
RPM_DEFAULTS: dict[str, dict[str, float | None]] = {
    POOL_FREE: {k: v for k, v in (("text", 20.0), ("image", 20.0), ("video", 1.0))},
    POOL_ENTERPRISE: {"text": 20.0, "image": 20.0, "video": 1.0},  # 待实采，暂取免费参考值
    POOL_TOKENPLAN: {"text": None, "image": None, "video": None},  # 不限速
}
# 日配额（免费按 RPM 自然封顶；TokenPlan 图 4000 张 / 视频 500 秒）
DAY_QUOTA_DEFAULTS: dict[str, dict[str, float | None]] = {
    POOL_FREE: {"text": None, "image": None, "video": None},
    POOL_ENTERPRISE: {"text": None, "image": None, "video": None},  # 待实采
    POOL_TOKENPLAN: {"text": None, "image": 4000.0, "video": 500.0},
}
# usage 计数键
USAGE_KEY = {"text": "text_n", "image": "img_n", "video": "video_s"}

REPO_ROOT = Path(__file__).resolve().parent.parent
ERROR_MAP_PATH = REPO_ROOT / "config" / "error_map.yaml"

# error_map 内建回退（与 config/error_map.yaml 同值；文件存在时以文件为准）
_FALLBACK_RULES = {
    "C": {"status": (401, 403), "keywords": ("invalid", "unauthorized", "revoked")},
    "B": {"status": (429, 403), "keywords": ("quota", "exhausted", "limit_reached")},
    "bare_429_backoff": 60.0,
}


class PoolExhausted(RuntimeError):
    """全池无可用 Key（熔断：调用方排队或直接 429 回前端）。"""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


# ---------- 脱敏 ----------

def mask_key(raw: str) -> str:
    """掩码：sk-前2~~~~后3（如 sk-9a~~~~7f3）。短 Key 降级处理，空返回空串。"""
    s = str(raw or "")
    if not s:
        return ""
    core = s[3:] if s.startswith("sk-") else s
    head = (core[:2] if len(core) >= 2 else core).ljust(2, "~")
    tail = s[-3:] if len(s) >= 3 else s
    return f"sk-{head}~~~~{tail}"


def mask_account(tag: str) -> str:
    """account_tag 脱敏（前端展示用；后端日志保留原文以便运维定位）。"""
    t = str(tag or "")
    if len(t) <= 2:
        return "*" * len(t)
    return f"{t[0]}***{t[-1]}"


# ---------- 数据结构 ----------

@dataclass
class KeyEntry:
    id: str
    mask: str
    raw_key: str = field(default="", repr=False)  # 仅内存，永不进日志/前端/DB明文列
    status: int = STATUS_IDLE                     # 2空闲 / 1使用中 / 0用光
    revoked: bool = False
    pool_type: str = POOL_FREE                    # free | enterprise | tokenplan
    account_tag: str = ""
    first_use_time: float = field(default_factory=time.time)
    usage: dict = field(default_factory=lambda: {"text_n": 0, "img_n": 0, "video_s": 0})
    limits: dict = field(default_factory=dict)    # {kind: {"rpm":..,"day_quota":..}}
    cooldown_until: float = 0.0
    lease_until: float = 0.0
    nonce: bytes = b""                            # 最近一次落盘密封的 nonce（审计用）
    shared_group: str | None = None
    tokens: dict = field(default_factory=dict, repr=False)       # 令牌桶余量 {kind: float}
    refill_ts: dict = field(default_factory=dict, repr=False)    # 桶上次补充时间


# ---------- Manager ----------

class KeyPoolManager:
    """线程安全 Key 池：threading.Lock 下原子化 acquire/release。"""

    def __init__(self, db_path: str | Path | None = None,
                 kek: bytes | bytearray | None = None,
                 error_map_path: str | Path | None = None):
        self._lock = threading.Lock()
        self._keys: dict[str, KeyEntry] = {}
        self._events: deque[tuple[float, str, str]] = deque()  # (ts, key_id, A/B)
        self._groups: dict[str, dict] = {}  # gid -> {tag, keys:set, last_linked, tokens, refill}
        self._db_path = Path(db_path) if db_path else None
        self._kek = bytes(kek) if kek is not None else None  # 仅内存驻留
        self._rules = self._load_error_map(error_map_path)
        self._stop = threading.Event()
        self._sweep_thread: threading.Thread | None = None

    # ----- 初始化 / 恢复 -----

    @classmethod
    def load(cls, db_path: str | Path, kek: bytes | bytearray,
             error_map_path: str | Path | None = None) -> "KeyPoolManager":
        """重启恢复：以 DB 为准全量 load；未到期租约视为过期回收（防卡死）。"""
        mgr = cls(db_path=db_path, kek=kek, error_map_path=error_map_path)
        if not _CRYPTO_OK:
            raise RuntimeError("缺少 backend.crypto，无法从加密 DB 恢复")
        assert _crypto is not None
        for rec in _crypto.load_key_records(db_path):
            try:
                # AAD 必须与 _persist 加密时一致（entry.id），否则 GCM 校验失败
                aad = str(rec.get("id", "") or "").encode("utf-8")
                raw = _crypto.decrypt_key(rec.get("nonce", ""), rec.get("ct", ""), bytes(kek), aad)
            except Exception as e:
                logger.error("event=recover key_mask=? account_tag=%s pool_type=%s "
                             "error=recover-decrypt-failed detail=%s",
                             rec.get("account_tag", ""), rec.get("pool_type", "free"),
                             type(e).__name__)
                continue
            usage = {}
            try:
                usage = json.loads(rec.get("usage_json") or "{}")
            except Exception:
                usage = {}
            meta = {}
            try:
                meta = json.loads(rec.get("meta_json") or "{}")
            except Exception:
                meta = {}
            entry = KeyEntry(
                id=rec.get("id", uuid.uuid4().hex[:12]),
                mask=mask_key(raw), raw_key=raw,
                status=int(rec.get("status", STATUS_IDLE)),
                revoked=bool(rec.get("revoked", False)),
                pool_type=rec.get("pool_type", POOL_FREE),
                account_tag=rec.get("account_tag", ""),
                first_use_time=float(meta.get("first_use_time", time.time())),
                limits=meta.get("limits", {}) or {},
                cooldown_until=float(meta.get("cooldown_until", 0.0)),
                lease_until=0.0,  # 未到期租约一律视为过期回收
                shared_group=meta.get("shared_group"),
                nonce=b"",
            )
            for k, dflt in (("text_n", 0), ("img_n", 0), ("video_s", 0)):
                entry.usage[k] = usage.get(k, dflt)
            if not entry.limits:
                entry.limits = self_default_limits(entry.pool_type)
            if entry.status == STATUS_BUSY:  # 重启回收租约
                entry.status = STATUS_IDLE
            mgr._keys[entry.id] = entry
        mgr._lazy_check(time.time())
        return mgr

    def unlock(self, kek: bytes | bytearray | None) -> None:
        """装载/更换内存 KEK（解锁 DB 写透）；lock() 清除。"""
        with self._lock:
            self._kek = bytes(kek) if kek is not None else None

    def lock(self) -> None:
        with self._lock:
            self._kek = None

    # ----- 注册 -----

    def register_key(self, raw_key: str, account_tag: str = "",
                     pool_type: str = POOL_FREE, key_id: str | None = None,
                     limits: dict | None = None) -> KeyEntry:
        if pool_type not in (POOL_FREE, POOL_ENTERPRISE, POOL_TOKENPLAN):
            raise ValueError(f"pool_type 非法：{pool_type}")
        if not raw_key:
            raise ValueError("raw_key 为空（正式版禁止写入明文空 Key）")
        with self._lock:
            entry = KeyEntry(
                id=key_id or uuid.uuid4().hex[:12],
                mask=mask_key(raw_key), raw_key=raw_key,
                pool_type=pool_type, account_tag=account_tag,
                limits=limits if limits is not None else self_default_limits(pool_type),
            )
            self._keys[entry.id] = entry
            self._persist(entry)
            self._log("register", entry)
            return entry

    # ----- acquire / release -----

    def acquire(self, kind: str, prefer_tag: str = "",
                exclude_tag: str = "", require_tag: str = "") -> KeyEntry:
        """取 Key：lazy_check（解冷/租约回收/24h重置）→ 跳 revoked/0/cooldown 中 →
        令牌桶 RPM → 日配额；命中则置 1 + 10min 租约 + 写透。无可用抛 PoolExhausted。

        归属隔离（docs/05 §5.9）：exclude_tag 命中的备注跳过（如 Agnes 不用
        “自建端点”密钥）；prefer_tag 命中的优先；require_tag 非空时只取该备注，
        无匹配直接抛 PoolExhausted（不回退，避免串用）。
        """
        if kind not in KINDS:
            raise ValueError(f"kind 非法：{kind}（仅 text/image/video）")
        now = time.time()
        with self._lock:
            self._lazy_check(now)
            self._dissolve_check(now)
            retry_after: float | None = None
            eligible: list[KeyEntry] = []
            for entry in self._keys.values():
                if entry.revoked or entry.status == STATUS_DRAINED:
                    continue
                if entry.cooldown_until > now:
                    left = entry.cooldown_until - now
                    retry_after = left if retry_after is None else min(retry_after, left)
                    continue
                if entry.status == STATUS_BUSY:
                    continue
                if exclude_tag and entry.account_tag == exclude_tag:
                    continue
                if require_tag and entry.account_tag != require_tag:
                    continue
                if not self._quota_ok(entry, kind):
                    continue
                if not self._bucket_probe(entry, kind, now):
                    continue
                eligible.append(entry)
            if require_tag and not eligible:
                raise PoolExhausted(
                    f"无可用 Key（kind={kind}，需备注={require_tag}）",
                    retry_after=retry_after)
            if prefer_tag:
                preferred = [e for e in eligible if e.account_tag == prefer_tag]
                if preferred:
                    eligible = preferred + [e for e in eligible
                                            if e.account_tag != prefer_tag]
            for entry in eligible:
                self._bucket_consume(entry, kind, now)
                entry.status = STATUS_BUSY
                entry.lease_until = now + LEASE_SECONDS
                self._persist(entry)
                self._log("acquire", entry, kind=kind)
                return entry
        raise PoolExhausted(f"无可用 Key（kind={kind}）", retry_after=retry_after)

    def release(self, key: KeyEntry | str, ok: bool, err: dict | None = None,
                cost: float = 1, kind: str = "text",
                latency_ms: float = 0.0) -> str:
        """归还 Key：累计用量（三类独立）→ A/B/C 分类 → 状态流转 → 写透 DB。

        返回分类标签：ok / A / B / C / unknown（unknown 按 ok 处理，只解租约）。
        cost 含义随 kind：text=次数，image=张数，video=秒数。
        """
        if kind not in KINDS:
            raise ValueError(f"kind 非法：{kind}")
        now = time.time()
        with self._lock:
            entry = key if isinstance(key, KeyEntry) else self._keys.get(key)
            if entry is None:
                raise KeyError("未知 key id")
            ukey = USAGE_KEY[kind]
            try:
                entry.usage[ukey] = float(entry.usage.get(ukey, 0)) + float(cost)
            except Exception:
                pass
            label = "ok" if ok else self.classify(err)
            retry_after: float | None = None
            if label == "A":
                retry_after = self._parse_retry_after((err or {}).get("retry_after"))
                if retry_after is None:
                    retry_after = float(self._rules.get("bare_429_backoff", 60.0))
                entry.cooldown_until = now + retry_after
                entry.status = STATUS_IDLE
                entry.lease_until = 0.0
                self._record_429(entry.id, now, label)
                self._log("cooldown", entry, kind=kind, cost=cost,
                          retry_after=retry_after, latency_ms=latency_ms, level=logging.WARNING)
            elif label == "B":
                entry.status = STATUS_DRAINED
                entry.lease_until = 0.0
                self._record_429(entry.id, now, label)
                self._log("exhausted", entry, kind=kind, cost=cost,
                          latency_ms=latency_ms, level=logging.WARNING)
            elif label == "C":
                entry.revoked = True
                entry.status = STATUS_IDLE
                entry.lease_until = 0.0
                self._log("revoked", entry, kind=kind, cost=cost,
                          latency_ms=latency_ms, level=logging.ERROR)
            else:  # ok / unknown：解租约；日配额到顶则进 0
                entry.status = STATUS_IDLE
                entry.lease_until = 0.0
                if not self._quota_ok(entry, kind):
                    entry.status = STATUS_DRAINED
                    self._log("exhausted", entry, kind=kind, cost=cost,
                              latency_ms=latency_ms, level=logging.WARNING)
                else:
                    self._log("release", entry, kind=kind, cost=cost,
                              latency_ms=latency_ms)
            self._persist(entry)
            return label

    # ----- 分类（以 error_map.yaml 为准） -----

    def _load_error_map(self, path: str | Path | None) -> dict:
        rules = {k: (dict(v) if isinstance(v, dict) else v)
                 for k, v in _FALLBACK_RULES.items()}
        p = Path(path) if path else ERROR_MAP_PATH
        try:
            if _YAML_OK and p.is_file():
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}  # type: ignore
                c = (data.get("C_revoked") or {}).get("match", {})
                b = (data.get("B_exhausted") or {}).get("match", {})
                if c.get("status"):
                    rules["C"] = {"status": tuple(c["status"]),
                                  "keywords": tuple(c.get("body_keywords", ())) or
                                              _FALLBACK_RULES["C"]["keywords"]}
                if b.get("status"):
                    rules["B"] = {"status": tuple(b["status"]),
                                  "keywords": tuple(b.get("body_keywords", ())) or
                                              _FALLBACK_RULES["B"]["keywords"]}
                dflt = (data.get("defaults") or {}).get("bare_429_backoff_seconds")
                if dflt is not None:
                    rules["bare_429_backoff"] = float(dflt)
        except Exception as e:
            logger.warning("event=config key_mask=- account_tag=- pool_type=- "
                           "error=error-map-load-failed detail=%s", type(e).__name__)
        return rules

    def classify(self, err: dict | None) -> str:
        """A/B/C/unknown/ok：顺序 C → B → A；无 Retry-After 的 429 默认 A。"""
        if not err:
            return "ok"
        try:
            status = int(err.get("status", 0))
        except Exception:
            status = 0
        body = str(err.get("body", "") or "").lower()
        rule_c, rule_b = self._rules["C"], self._rules["B"]
        if status in rule_c["status"] and any(k in body for k in rule_c["keywords"]):
            return "C"
        if status in rule_b["status"] and any(k in body for k in rule_b["keywords"]):
            return "B"
        if status == 429:
            return "A"
        return "unknown"

    @staticmethod
    def _parse_retry_after(value: Any) -> float | None:
        try:
            if value is None or value == "":
                return None
            v = float(value)
            return v if v >= 0 else None
        except Exception:
            return None

    # ----- 内部：懒检查 / 桶 / 配额 -----

    def _lazy_check(self, now: float) -> None:
        for entry in self._keys.values():
            if entry.status == STATUS_BUSY and entry.lease_until and entry.lease_until <= now:
                entry.status = STATUS_IDLE  # 租约过期强制回收
                entry.lease_until = 0.0
                self._persist(entry)
            if now - entry.first_use_time >= DAY_SECONDS:  # 24h 滚动重置
                entry.first_use_time = now
                entry.usage = {"text_n": 0, "img_n": 0, "video_s": 0}
                entry.cooldown_until = 0.0
                if entry.status == STATUS_DRAINED and not entry.revoked:
                    entry.status = STATUS_IDLE
                self._persist(entry)

    def _rpm(self, entry: KeyEntry, kind: str) -> float | None:
        try:
            return entry.limits.get(kind, {}).get("rpm")
        except Exception:
            return None

    def _bucket_state(self, entry: KeyEntry, kind: str, now: float) -> list:
        """返回 [tokens_dict, refill_dict]：共享组内用组桶串行，否则用 Key 独立桶。"""
        if entry.shared_group and entry.shared_group in self._groups:
            g = self._groups[entry.shared_group]
            return [g["tokens"], g["refill"]]
        return [entry.tokens, entry.refill_ts]

    def _bucket_probe(self, entry: KeyEntry, kind: str, now: float) -> bool:
        rpm = self._rpm(entry, kind)
        if rpm is None:  # TokenPlan 不限速
            return True
        tokens, refill = self._bucket_state(entry, kind, now)
        cur = float(tokens.get(kind, rpm))
        cur = min(float(rpm), cur + (now - float(refill.get(kind, now))) * (float(rpm) / 60.0))
        return cur >= 1.0

    def _bucket_consume(self, entry: KeyEntry, kind: str, now: float) -> None:
        rpm = self._rpm(entry, kind)
        if rpm is None:
            return
        tokens, refill = self._bucket_state(entry, kind, now)
        cur = float(tokens.get(kind, rpm))
        cur = min(float(rpm), cur + (now - float(refill.get(kind, now))) * (float(rpm) / 60.0))
        tokens[kind] = cur - 1.0
        refill[kind] = now

    def _quota_ok(self, entry: KeyEntry, kind: str) -> bool:
        try:
            quota = entry.limits.get(kind, {}).get("day_quota")
        except Exception:
            return True
        if quota is None:
            return True
        return float(entry.usage.get(USAGE_KEY[kind], 0)) < float(quota)

    # ----- 共享池探测 -----

    def _record_429(self, key_id: str, now: float, label: str) -> None:
        self._events.append((now, key_id, label))
        cutoff = now - SHARED_WINDOW
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()
        self._merge_check(now)

    def _merge_check(self, now: float) -> None:
        by_tag: dict[str, set[str]] = {}
        for _ts, kid, _label in self._events:
            e = self._keys.get(kid)
            if e is None:
                continue
            by_tag.setdefault(e.account_tag or "__untagged__", set()).add(kid)
        for tag, kids in by_tag.items():
            if len(kids) < 2:  # 60s 窗口 ≥2 Key 联动 429 才归并
                continue
            for gid, g in self._groups.items():
                if len(g["keys"] & kids) >= 2:
                    g["last_linked"] = now
                    break
            else:
                gid = f"g-{abs(hash(tag)) % 10000:04d}-{int(now) % 100000}"
                members = [self._keys[k] for k in kids if k in self._keys]
                gtokens: dict[str, float] = {}
                grefill: dict[str, float] = {}
                for kind in KINDS:  # 组桶取成员最小 RPM（串行），全 None 则不限速
                    rpms = [self._rpm(m, kind) for m in members]
                    rpms = [r for r in rpms if r is not None]
                    if rpms:
                        gtokens[kind] = min(rpms)
                        grefill[kind] = now
                self._groups[gid] = {"tag": tag, "keys": set(kids),
                                     "last_linked": now, "tokens": gtokens,
                                     "refill": grefill}
                for k in kids:
                    if k in self._keys:
                        self._keys[k].shared_group = gid
                        self._persist(self._keys[k])
                masks = ",".join(self._keys[k].mask for k in kids if k in self._keys)
                logger.warning("event=merge key_mask=%s account_tag=%s pool_type=shared "
                               "error=shared-pool-merged detail=gid=%s",
                               masks, tag, gid)

    def _dissolve_check(self, now: float) -> None:
        for gid in [g for g, v in self._groups.items()
                    if now - v.get("last_linked", now) > SHARED_DISSOLVE]:
            g = self._groups.pop(gid)
            for k in g.get("keys", set()):
                if k in self._keys and self._keys[k].shared_group == gid:
                    self._keys[k].shared_group = None
                    self._persist(self._keys[k])
            logger.info("event=sweep key_mask=- account_tag=%s pool_type=shared "
                        "error=shared-pool-dissolved detail=gid=%s", g.get("tag", ""), gid)

    def detect_shared_pool(self, now: float | None = None) -> list[str]:
        """公开触发： stage 探测（归并/解散各跑一次），返回活跃组 id。"""
        ts = now if now is not None else time.time()
        with self._lock:
            cutoff = ts - SHARED_WINDOW
            self._events = deque(e for e in self._events if e[0] >= cutoff)
            self._merge_check(ts)
            self._dissolve_check(ts)
            return list(self._groups.keys())

    # ----- sweep 后台 -----

    def sweep(self, now: float | None = None) -> dict:
        """单次 sweep：懒检查 + 解散判定 + 批量写透（后台线程与启动恢复共用）。"""
        ts = now if now is not None else time.time()
        with self._lock:
            before = {k: (e.status, e.cooldown_until) for k, e in self._keys.items()}
            self._lazy_check(ts)
            self._dissolve_check(ts)
            changed = sum(1 for k, e in self._keys.items()
                          if before.get(k) != (e.status, e.cooldown_until))
            self._log_sweep(len(self._keys), changed)
            return {"keys": len(self._keys), "changed": changed,
                    "groups": list(self._groups.keys())}

    def start_background(self, interval: float = SWEEP_INTERVAL) -> None:
        if self._sweep_thread and self._sweep_thread.is_alive():
            return
        self._stop.clear()

        def _loop() -> None:
            while not self._stop.wait(interval):
                try:
                    self.sweep()
                except Exception as e:
                    logger.warning("event=sweep key_mask=- account_tag=- pool_type=- "
                                   "error=sweep-failed detail=%s", type(e).__name__)

        self._sweep_thread = threading.Thread(target=_loop, name="keypool-sweep",
                                              daemon=True)
        self._sweep_thread.start()

    def stop_background(self) -> None:
        self._stop.set()
        if self._sweep_thread:
            self._sweep_thread.join(timeout=5)
            self._sweep_thread = None

    # ----- 持久化 / 前端 / 日志 -----

    def _persist(self, entry: KeyEntry) -> bool:
        """Write-Through：acquire 设 1+租约、release 状态流转、冷却/用光/作废/归并立即落盘。

        无 db_path 或未 unlock(KEK) 时为纯内存模式，返回 False。
        """
        if self._db_path is None or self._kek is None or not _CRYPTO_OK:
            return False
        try:
            assert _crypto is not None
            sealed = _crypto.encrypt_key(entry.raw_key, self._kek,
                                         aad=entry.id.encode("utf-8"))
            entry.nonce = __import__("base64").b64decode(sealed["nonce_b64"])
            _crypto.store_key_record({
                "id": entry.id, "account_tag": entry.account_tag,
                "pool_type": entry.pool_type, "status": entry.status,
                "revoked": entry.revoked, "nonce": sealed["nonce_b64"],
                "ct": sealed["ct_b64"], "usage_json": json.dumps(entry.usage),
                "meta_json": json.dumps({
                    "first_use_time": entry.first_use_time,
                    "cooldown_until": entry.cooldown_until,
                    "lease_until": entry.lease_until,
                    "limits": entry.limits,
                    "shared_group": entry.shared_group}),
            }, self._db_path)
            return True
        except Exception as e:
            logger.error("event=persist key_mask=%s account_tag=%s pool_type=%s "
                         "error=persist-failed detail=%s",
                         entry.mask, entry.account_tag, entry.pool_type, type(e).__name__)
            return False

    def has_live_key(self) -> bool:
        """是否有未作废、未用光的 Key（不看桶/冷却/租约）。

        供调用方区分“等一等就有”（桶空/冷却中→等待重试）和
        “真全灭”（全作废/用光→直接失败）。读锁内快照。
        """
        with self._lock:
            return any(not e.revoked and e.status != STATUS_DRAINED
                       for e in self._keys.values())

    def custom_endpoint_key_status(self) -> dict:
        """自建端点专用密钥状态（仅掩码）：{configured, mask}。"""
        with self._lock:
            for e in self._keys.values():
                if e.account_tag == CUSTOM_ENDPOINT_TAG and not e.revoked:
                    return {"configured": True, "mask": e.mask}
        return {"configured": False, "mask": None}

    def to_frontend_dict(self, now: float | None = None) -> list[dict]:
        """前端展示：只吐掩码字段（mask/状态/pool_type/用量/限额/冷却倒计时/脱敏账号）。

        永不含 raw_key / nonce / ct。
        """
        ts = now if now is not None else time.time()
        out = []
        with self._lock:
            for e in self._keys.values():
                cooling = max(0.0, e.cooldown_until - ts)
                if e.revoked:
                    display = "作废"
                elif cooling > 0:
                    display = "冷却中"
                else:
                    display = {STATUS_IDLE: "空闲", STATUS_BUSY: "使用中",
                               STATUS_DRAINED: "用光"}.get(e.status, str(e.status))
                out.append({
                    "mask": e.mask, "status": e.status, "display": display,
                    "revoked": e.revoked, "pool_type": e.pool_type,
                    "account_tag": mask_account(e.account_tag),
                    "usage": dict(e.usage), "limits": json.loads(json.dumps(e.limits)),
                    "cooldown_left_s": round(cooling, 1),
                    "shared_group": e.shared_group,
                })
        return out

    def _log(self, event: str, entry: KeyEntry, kind: str = "",
             cost: Any = 0, retry_after: Any = None,
             latency_ms: Any = 0.0, level: int = logging.INFO) -> None:
        logger.log(level,
                   "time=%.3f key_mask=%s account_tag=%s pool_type=%s event=%s "
                   "kind=%s cost=%s retry_after=%s latency_ms=%s",
                   time.time(), entry.mask, entry.account_tag, entry.pool_type,
                   event, kind, cost,
                   "" if retry_after is None else retry_after, latency_ms)

    def _log_sweep(self, total: int, changed: int) -> None:
        logger.info("time=%.3f key_mask=- account_tag=- pool_type=- event=sweep "
                    "kind=- cost=%s retry_after=- latency_ms=-",
                    time.time(), f"{changed}/{total}")


def self_default_limits(pool_type: str) -> dict:
    """按 pool_type 注入默认 limits（RPM + 日配额，见 RPM_DEFAULTS / DAY_QUOTA_DEFAULTS）。"""
    rpm = RPM_DEFAULTS.get(pool_type, RPM_DEFAULTS[POOL_FREE])
    quota = DAY_QUOTA_DEFAULTS.get(pool_type, DAY_QUOTA_DEFAULTS[POOL_FREE])
    return {k: {"rpm": rpm.get(k), "day_quota": quota.get(k)} for k in KINDS}
