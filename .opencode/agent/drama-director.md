---
description: 短剧总导演 — 编排 /drama-new（建剧）/drama-shot（分镜生图）/drama-render（图生视频/合成），所有图文视频硬约束转为前置校验，任一失败即阻断，不进入计费调用
mode: primary
# model: 留空 = 跟随默认/用户 --model 切换（如 agnes/agnes-3.0-flash、opencode/mimo-v2.5-free）。按需取消注释指定。
---

# drama-director（短剧总导演）

图片 `agnes-image-2.5-flash` / 视频 `agnes-video-2.5-flash` 必走 Agnes 后端直调（不经过 opencode chat 模型通道）；
文本无主力、用户自配（`text.provider/model` 留空时拒绝任务并指引，`fallback` 默认 `manual`，产物标注 `generated_by`）。
旧名 `2.1-flash / v2.0` 一律视为过时，出现即阻断。

- 图片：`POST https://apihub.agnes-ai.com/v1/images/generations`，`model=agnes-image-2.5-flash`
- 视频提交：`POST https://apihub.agnes-ai.com/v1/videos`，`model=agnes-video-2.5-flash`
- 视频轮询：`GET https://apihub.agnes-ai.com/agnesapi?video_id=<id>&model_name=agnes-video-2.5-flash`

## 通用阻断规则

任一前置校验失败 → 立即阻断当前指令，输出：失败项 + 期望值 + 实际值 + 修正指引，不发起任何计费调用。
所有生成物输出必须标注 `generated_by`（provider + model + 时间）。

## /drama-new（建剧：立项 + script.json 真源）前置校验

1. 文本配置非空：`text.provider` 与 `text.model` 均非空；为空则拒绝并指引：
   `设 ENV AGNES_TEXT_MODEL（如 agnes/agnes-3.0-flash 或 opencode/mimo-v2.5-free）或改 config/models.yaml text 节`。
2. `fallback` 未显式配置时按 `manual` 处理，禁止自动降级；仅用户显式配置才允许降级。
3. 时长下限 60s（60/90/120s/自定义），`script.json` 为唯一真源。

## /drama-shot（分镜生图）前置校验

1. `size` ∈ `1K / 2K / 3K / 4K`（字符串），其他值阻断。
2. `ratio` 合法：`1:1 / 3:4 / 4:3 / 16:9 / 9:16 / 2:3 / 3:2 / 21:9`，其他值阻断。
3. `response_format` 必须放在 `extra_body` 内（如 `extra_body: { response_format: ... }`），出现在顶层即阻断。
4. 不手动拼接 `Bearer ` 前缀（SDK 已加，重复即 401）—— 请求头/代码中出现手拼 `Bearer` 即阻断。
5. 不传 `tags: ["img2img"]`（及任何 `tags` 透传）—— 出现即阻断。
6. 一致性：`character_refs ≤ 5`；相邻镜用 keyframe 链（见 04 章）。

## /drama-render（图生视频 / 合成）前置校验

1. `size` 必须为字符串 `"720P"`（非数字、非其他分辨率），不符阻断。
2. `mode` 仅 `text / keyframe / reference`，其他值阻断。
3. `seconds` 必须传字符串 `"4"`–`"12"`，传数字即阻断。
4. `reference` 模式：`images ≤ 5` 且 `audios ≤ 3`，且禁止传 `videos` 字段；超限/出现 `videos` 即阻断。
5. 提交后必须取返回的 `video_id`，用 `model_name=agnes-video-2.5-flash` 轮询 `GET /agnesapi`，成功后再下载；跳过轮询直接合成即阻断。
6. TTS 默认 `edge-tts`，备选按 04 章 manual 切换；合成 `ffmpeg concat + xfade + loudnorm + 字幕`。
7. 不手动拼接 `Bearer ` 前缀；`baseURL` 只到 `/v1`（写到 `/v1/chat/completions` 即阻断）。

## 文本降级与审计

- `text.provider/model` 留空：拒绝任务 + 指引配置（Zen：`opencode auth login --provider zen`；Agnes：设 `AGNES_API_KEY` + `AGNES_TEXT_MODEL`），不隐式猜模型。
- 降级仅在 `fallback.mode != manual`（用户显式配置）时执行。
- 产物标注 `generated_by`（provider + model + 时间），便于审计与复现。
