# free-ai-video 项目文档索引

> 零成本 AI 短剧客户端：Vue3 SPA + 本地 FastAPI。图片 `agnes-image-2.5-flash` / 视频 `agnes-video-2.5-flash` 必走 Agnes；文本无主力、用户自配。

| 章 | 文档 | 内容 |
|---|---|---|
| 01 | [总览与系统架构](01-overview-architecture.md) | 愿景/分层/模块/数据流/目录与 ADR |
| 02 | [多Key池生命周期与调度](02-keypool-scheduling.md) | 独立池/状态机/RPM桶/429三级/24h刷新/共享池探测 |
| 03 | [加密与激活](03-encryption-activation.md) | KEK派生/TPM+DPAPI/Env规范/轮换/威胁模型 |
| 04 | [短剧流水线与TTS](04-drama-pipeline-tts.md) | script.json/60s+估算/一致性/TTS矩阵/ffmpeg/重试 |
| 05 | [opencode与模型配置](05-opencode-config.md) | 双源注册/models/全开放配置/drama-director校验 |

参考：根目录 `API密钥池生命周期管理与安全调度策略设计文档(C++版).md`（C++ 版原型思想，Python 落地时加密算法已升级为 AES-256-GCM）。

## 全局锁定项

- 短剧下限 60s，用户自选 60/90/120s/自定义；免费视频 1 RPM 下需队列串行。
- 多免费 Key 默认不同账号、独立池；同账号同类型共享池不叠加。
- 文本 `fallback` 默认 manual；产物标注 `generated_by`。
- 前端只见掩码 `sk-前2~~~~后3`；KEK/明文不出内存。
