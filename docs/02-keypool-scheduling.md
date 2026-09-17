# 02 多Key池生命周期与调度

> 本章阈值与报文分类均为**启发式、待实采校准**，不虚构 Agnes 未公开报文；一切以线上实采 `status + Retry-After + body关键词` 回填 `error_map.yaml` 为准。

## 2.1 三层架构（Python化）

借鉴 C++ 文档思想，Python 实现为三层：

1. **加密 DB（Single Source of Truth）**：`keys_encrypted.db`，`raw_key` 以 **AES-256-GCM + PBKDF2** 落盘（不用 DES demo），进程重启唯一恢复源；
2. **内存 Lock 池**：`KeyPoolManager` 持 `threading.Lock`，所有 `acquire/release` 原子化；
3. **前端掩码层**：SSE/轮询只推 `sk-前2~~~~后3`，永不下发原文。

核心结论：**同账号同类型多 Key 共享池、不叠加、不提速**；不同类型（免费/企业/TokenPlan）独立池。本项目默认 `多免费 Key = 不同账号 = 独立池`，另加探测纠偏。

## 2.2 状态机（含冷却态）

`status: 2空闲 / 1使用中 / 0用光`，外加 `cooldown_until` 瞬时冷却与 `revoked` 作废。

```text
[*] --> 空闲2: 注册/重置24h
空闲2 --> 使用中1: acquire+租约10min
使用中1 --> 空闲2: release成功
使用中1 --> 冷却态: A瞬时429+Retry-After，设cooldown_until
冷却态 --> 空闲2: 到期自动解冷
使用中1 --> 用光0: B配额用光 quota/exhausted
用光0 --> 空闲2: first_use_time+24h滚动重置
使用中1 --> 作废revoked: C401/invalid，人工处理
使用中1 --> 空闲2: 租约10min过期强制回收
```

`1使用中` 必须带租约（如 10min）防任务卡死；`acquire` 前懒检查 + 后台 60s `sweep` 做 24h 滚动重置。

## 2.3 数据结构

| 字段 | 说明 |
|---|---|
| `id / raw_key` | 主键/原文，仅内存+加密 DB 可见 |
| `status` | `2/1/0`，另见 `revoked` 布尔 |
| `first_use_time` | 24h 窗口起点，滚动刷新依据 |
| `usage {text_n, img_n, video_s}` | 文本次数/图片张数/视频秒数，独立累计 |
| `limits {rpm, day_quota}` | 按 `pool_type` 注入（见 2.4） |
| `cooldown_until` | A 类限流冷却到期时间 |
| `revoked` | C 类作废，需人工解绑 |
| `account_tag / pool_type` | 账号归属标签/`free\|enterprise\|tokenplan` |

## 2.4 acquire/release 伪代码

```python
def acquire(kind):  # kind=text/image/video
    with lock:
        lazy_check(now)  # 过期冷却解冷、过期租约回收、24h重置
        for k in pool.filter(pool_type=kind.pool):
            if k.revoked or k.status == 0:
                continue
            if k.cooldown_until > now:
                continue
            if not token_bucket_allow(k, kind):
                continue
            k.status = 1
            k.lease_until = now + 10 * 60
            write_through(k)
            return k
    raise PoolExhausted

def release(k, ok, err, cost):
    with lock:
        k.usage += cost  # 三类独立加
        classify(k, err)  # A/B/C
        if ok or is_A(err):
            k.status = 2
        write_through(k)
```

## 2.5 RPM 令牌桶与日配额

令牌桶按 `pool_type + kind` 限速，日配额按 `first_use_time` 24h 清零。

| 池类型 | 文本 RPM | 图片 RPM | 视频 RPM | 日配额（启发式） |
|---|---|---|---|---|
| 免费 | 20 | 20（1K 图） | 1 | 按 RPM 自然封顶 |
| TokenPlan | 不限速* | 不限速* | 不限速* | 图 4000 张 / 视频 500 秒 |
| 企业 | 待实采 | 待实采 | 待实采 | 待实采 |

*TokenPlan 瓶颈在日配额而非 RPM；**同账号多 Key 不提速**。

## 2.6 error_map.yaml 与 429 三级

```yaml
# 启发式、待实采校准；只依赖 status+头+关键词
A_cooldown:  # 瞬时限流：冷却，不进0，切下一Key重试
  match: {status: 429, has_retry_after: true}
  action: {set: cooldown_until=now+RetryAfter, status: 2}
B_exhausted:  # 配额用光：进0等24h
  match: {status: [429, 403], body_keywords: [quota, exhausted, limit_reached]}
  action: {set: status=0}
C_revoked:  # 作废：人工处理
  match: {status: [401, 403], body_keywords: [invalid, unauthorized, revoked]}
  action: {set: revoked=true}
```

无 `Retry-After` 的 429 默认走 A 并退避 60s，避免误杀。

## 2.7 共享池探测算法

默认独立池；若同 `account_tag` 或未标记的多 Key 在**同分钟齐 429**，自动归并串行：

1. 滑动窗口 60s 内统计 `A/B` 次数；
2. 若 `≥2 Key` 同时触发，则打标 `shared_group=<gid>`，组内退化为单令牌桶串行调度；
3. 连续 30min 无联动 429 则自动解散，恢复独立池；每次归并/解散 `Write-Through`。

## 2.8 持久化与 Write-Through 时机

DB：`keys_encrypted.db`（SQLCipher/AES-GCM 二选一，PBKDF2 派生）。时机：`acquire` 设 1+租约、`release` 累计用量与状态流转、冷却/用光/作废/探测归并立即落盘；`sweep` 批量刷 24h 重置。启动时全量 `load` 重建内存池，保证重启恢复。

## 2.9 前端展示字段

只推：`mask`、`status(2/1/0/冷却中/作废)`、`pool_type`、`usage/limits` 进度条、`cooldown_until` 倒计时、`account_tag` 脱敏。SSE 主推 + 轮询兜底（30s）。

## 2.10 异常与可观测

* **全池耗尽**：熔断排队（有界队列 + 预估等待 `min(cooldown)`），队列满直接 429 回前端；
* **重启恢复**：以 DB 为准，未到期租约视为过期回收，避免卡死；
* **日志字段**：`time, key_mask, account_tag, pool_type, event(acquire/release/cooldown/exhausted/revoked/sweep/merge), kind, cost, retry_after, latency_ms`，冷却与归并必须 `WARN`。
