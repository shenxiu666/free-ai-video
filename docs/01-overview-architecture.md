# 01 总览与系统架构

## 1.1 愿景与非目标

**愿景：** 零成本 AI 短剧客户端。用户在本地浏览器完成“题材 → 成片”，图片与视频默认走 Agnes 免费模型，TTS 默认走本地/免费方案，全链路可在单机跑通 60s 以上短剧。

**非目标（本期不做）：**

- 不做服务端多租户、云端账号体系与计费，以后再考虑服务端；
- 不自研大模型，不承诺 Agnes 免费额度数字，具体配额/RPM 以官方文档为准（参考值、可能调整）；
- opencode CLI 仅为开发期 Agent（代码生成/调试），运行时不强依赖，缺失不影响出片。

## 1.2 用户角色

- **创作者：** 新建剧、改分镜、触发渲染、导出 `final.mp4`；
- **运维者（同人兼任）：** 在“密钥池+设置”页维护多 Key、切换文本/TTS 模型、查看渲染队列与 SSE 日志。

## 1.3 系统分层

客户端软件 = `Vue3 SPA` + `本地 FastAPI 后端`，仅经 `localhost REST+SSE` 通信，纯离线可跑（除调用云模型外）。

```text
[Vue3 SPA] --REST/SSE--> [FastAPI: localhost:8000]
   新建剧/分镜表/       KeyPool | Providers | Orchestrator
   渲染队列/密钥池+设置    TTS | FFmpeg | state.json 落盘
        |                        |
        +------> outputs/<剧名>/ <------+
```

- 前端只做编排与展示，不直调 Agnes，不存 Key 明文；
- 后端持有密钥池、模型适配、任务状态机与 FFmpeg 合成。

## 1.4 模块清单

**前端 4 页：**

1. 新建剧：题材、时长（60/90/120s/自定义，下限 60s）、风格；
2. 分镜表：编辑 `script.json`（分镜/台词/镜头/时长）；
3. 渲染队列：任务进度 + SSE 日志 + 断点续跑/重试；
4. 密钥池+设置：多 Key 管理、文本/TTS/图/视频模型参数。

**后端 5 模块：**

- `KeyPool`：多免费 Key 默认不同账号、独立池（image/video/text 分池），轮询+熔断+24h 刷新，前端仅见脱敏；
- `Providers`：Agnes 图/视频、OpenAI 兼容文本适配；
- `Orchestrator`：流水线状态机，写 `state.json`；
- `TTS`：默认 `edge-tts`，备选 Kokoro-82M / CosyVoice3 / Qwen3-TTS / IndexTTS2；
- `FFmpeg`：拼装视频+配音+字幕为 `final.mp4`。

## 1.5 端到端数据流

```text
题材 -> 大纲 -> script.json -> 图 -> 视频 -> 配音 -> 合成
```

1. 文本模型出大纲/分镜 → `script.json`（唯一可编辑真相）；
2. `POST /v1/images/generations` 调 `agnes-image-2.5-flash`（`size`：1K/2K/3K/4K + `ratio`，现价 $0）生关键帧；
3. `POST /v1/videos` + `GET /agnesapi?video_id=&model_name=` 调 `agnes-video-2.5-flash`（`size` 固定 `720P` 字符串，`seconds` 字符串 4-12，`mode`：text/keyframe/reference，现价 $0）；
4. TTS 生成台词音频 → FFmpeg 按分镜时长混流合成。

文本无主力、用户自配（Agnes / opencode 免费 / 自建 OpenAI 兼容），留空则拒绝任务并提示去设置页填写。

## 1.6 目录与产物约定

```text
config/models.yaml      # 全模型与参数默认值
backend/                # FastAPI + KeyPool/Providers/Orchestrator/TTS/FFmpeg
frontend/               # Vue3 SPA
outputs/<剧名>/
  script.json  images/  videos/  audio/
  final.mp4    state.json   # final.mp4 恒为最新版指针；每次合成另存 成片/final_时间戳.mp4 全部保留
```

全开放自定义：`config/models.yaml + ENV + CLI` 三层覆盖所有模型与参数，优先级 `CLI > ENV > YAML`。

断点续跑：`state.json` 记录每分镜 `pending/doing/done/failed` 与产物哈希；重启以后缀产物存在性 + `state.json` 为准，未完成分镜可单独重试，不重跑全剧。

## 1.7 关键决策表

| ID | 决策 | 理由 |
|----|------|------|
| ADR-1 | Vue3 SPA + 本地 FastAPI，localhost REST+SSE，暂无服务端 | 单机零成本、可移植，服务端后续再加 |
| ADR-2 | 图 `agnes-image-2.5-flash` / 视频 `agnes-video-2.5-flash`，文本无主力用户自配 | 图/视频现价 $0 可用，文本分歧大故开放 |
| ADR-3 | `models.yaml + ENV + CLI` 三层覆盖 | 默认可跑、高级用户全参可调 |
| ADR-4 | 多 Key 独立池；Env + 后端长期派生密钥 + TPM，无 TPM 用 DPAPI 兜底，纯离线 | 防串池、落盘必加密，不出内网 |
| ADR-5 | 成片下限 60s（60/90/120s/自定义）；TTS 默认 edge-tts + 4 备选 | 保短剧体量，配音免费可切换 |
| ADR-6 | 以 `outputs/<剧名>/ + state.json` 为真相源，无 DB | 文件即产物，拷贝即备份，天然断点续跑 |
