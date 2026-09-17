# 04 短剧流水线与TTS

## 4.1 目标与输入约束

本章定义60s+短剧的端到端流水线：`剧本→script.json→配音TTS→分镜图→分镜视频→合成输出`。

硬约束：短剧下限60s，用户自选60/90/120s/自定义时长；画幅默认9:16（720x1280），横屏16:9可选；视频单clip的`seconds`为字符串，取值4-12，推荐6-8镜×8-10s拼60s；镜数=总秒/单镜秒，向上取整，末镜不足则补足或截断对齐。

## 4.2 script.json契约

`script.json`为流水线唯一真源，TTS/绘图/视频/合成均由此驱动。

```json
{
  "title": "重生之我在免费池修仙",
  "total_seconds": 60,
  "aspect": "9:16",
  "resolution": "720x1280",
  "clip_seconds": "8",
  "character_refs": ["https://.../char1.png"],
  "clips": [
    {
      "id": "s01",
      "start": 0,
      "duration": 8,
      "narration": "三年前，他被逐出师门。",
      "srt_path": "tts/s01.srt",
      "image_prompt": "[主体]白衣少年山门前+[场景]雪夜石阶+[风格]电影感写实+[光照]冷月顶光+[构图]竖构图全身+[质量]1K,高细节",
      "video_prompt": "[主体]白衣少年+[动作]抬头转身+[场景]雪夜石阶+[运镜]缓慢推镜+[光照]冷月光+[风格]电影感",
      "video_mode": "keyframe",
      "first_frame": "shots/s00_last.png"
    }
  ]
}
```

校验规则：`total_seconds>=60`；`clip_seconds in ["4".."12"]`；`sum(clips.duration)==total_seconds`；`character_refs.length<=5`。

## 4.3 分镜时序估算

按默认单镜8s，镜数=总秒/8。耗时瓶颈为视频RPM，图片1K可提前批量备好，视频必须排队。

| 总时长 | 分镜数 | API调用（图+视频） | 免费1RPM | Plan 5RPM |
|---|---|---|---|---|
| 60s | 8镜×8s | 16次 | 约8-10min | 约2-3min |
| 90s | 12镜×8s | 24次 | 约12-16min | 约3-5min |
| 120s | 15镜×8s | 30次 | 约16-22min | 约4-7min |

免费1RPM下60s约6min+为理论下限，实际含生成等待、轮询、合成需上浮30%，必须进队列串行调度并提示用户等待。

## 4.4 图像与视频生成规范

**图像（agnes-image-2.5-flash）：** `size`只用`1K/2K/3K/4K`+`ratio`组合，短剧默认`1K`降本提速；`extra_body.response_format:url`必须放`extra_body`内，禁顶层；图生图/多图引用走`extra_body.image`；不传`tags`；超时设60-360s；`image_prompt`固定六段式：[主体+场景+风格+光照+构图+质量]。

**视频（agnes-video-2.5-flash）：** `size`固定`"720P"`；`mode`三态：`text`禁带任何媒体字段，`keyframe`需`first_frame/last_frame`至少其一，`reference`需`images(≤5)/audios(≤3)`至少一类非空且禁`videos`；`seconds`传字符串；提交后轮询`GET /agnesapi?video_id=&model_name=agnes-video-2.5-flash`，1-2s一次直至`completed/failed`；`video_prompt`六段式：[主体+动作+场景+运镜+光照+风格]，`reference`模式引用图用`<Picture N>`指代。

## 4.5 一致性策略

1. **全局角色锚点：** `character_refs≤5`张立绘复用为各镜`reference.images`，保证主角脸/服饰跨镜一致，不每镜重造人设。
2. **相邻链式衔接：** `prev.last_frame→next.first_frame`的`keyframe`链，下一镜首帧沿用上一镜尾帧，消除跳切与场景漂移。
3. **提示词继承：** 后镜`image/video_prompt`继承前镜[主体+风格+光照]前缀，仅改[动作/运镜/场景增量]。

## 4.6 TTS统一接口与选型

统一接口：`synthesize(provider, voice, text, srt) -> (wav, srt)`，上层只依赖`provider/voice/srt`三字段，策略为manual，默认不自动切换，仅失败时人工确认后改`provider`重跑。

| 方案 | 离线/在线 | 优势 | 短板 | 适用 |
|---|---|---|---|---|
| edge-tts（默认） | 在线免费 | 免费、中文好、无部署 | 需网络、无克隆 | 默认旁白 |
| Kokoro-82M | CPU离线 | 离线兜底、82M轻量 | 音色少 | 断网兜底 |
| CosyVoice3 | 本地/云 | 高仿克隆 | 部署重 | 主角克隆 |
| Qwen3-TTS | 云 | 商用友好 | 收费 | 商用发行 |
| IndexTTS2 | 本地 | 时长卡点准 | license细看 | 对口型卡点 |

选择流程：默认edge-tts→需离线选Kokoro-82M→需克隆选CosyVoice3→需商用选Qwen3-TTS→需严格对齐单镜时长选IndexTTS2（先审license）。

## 4.7 ffmpeg合成链

`concat(硬切拼接)→xfade 0.3s(镜间淡化)→配音按镜对齐混入(amix)→loudnorm(响度归一)→subtitles烧录srt`。先`concat`保证总时长=剧本时长，再每接缝`xfade:duration=0.3`；配音 `audio/<镜>.wav` 按剧本 `start` 秒 `adelay` 对齐后与原音频 `amix=inputs=N:duration=first`（输出时长恒等于画面，缺 wav 的镜自动跳过，超长配音 bleed 进下一镜不断尾），最后`-vf subtitles=full.srt`烧录字幕。每次合成写独立版本 `成片/final_YYYYMMDD-HHMMSS.mp4`（全部保留），`final.mp4` 恒为最新版指针（拷贝），`state.json` 的 `finals` 数组留痕（近 50 条，记相对路径如 `成片/final_….mp4`）。字幕必须链在视频输出 label 上（缀音频链会报 pad 绑定失败，见 mem 2026-09-17）。

## 4.8 状态与失败重试矩阵

`state.json`记录每镜`image/video/tts/mux: pending/done/failed`，断点续跑，单镜失败只重跑该镜。

| 失败 | 判定 | 重试 |
|---|---|---|
| 图片超时 | 60-360s无返回 | 保留prompt，仅重跑该镜，加超时档 |
| 视频failed | 轮询到failed | 检查mode字段合规后重提该镜 |
| 429限流 | 1RPM超限 | 队列退避60s，不并行提速 |
| TTS失败 | 无wav/srt | 同voice重试1次，再转manual换provider |
| ffmpeg失败 | 非0退出 | 不重跑生成，仅修srt/路径后重mux |

## 4.9 验收标准

1. 60/90/120s成品时长误差<±1s，竖屏720x1280，字幕与配音对齐；
2. 相邻镜人物一致性人工抽检通过，无跳脸，主要转场有0.3s xfade；
3. 任意单镜失败后重跑`state.json`可续跑且不重跑成功镜；
4. 免费1RPM下任务可排队完成，Plan 5RPM耗时符合4.3表。

## 4.10 AI 分镜（小说/剧本 → 初稿，用户可改）

分镜表不再手写：新建页粘贴小说/剧本原文 → `POST /api/drama/breakdown` → 文本模型按固定 prompt 输出严格 JSON → 服务端归一化并经 `validate_script` 校验 → 分镜表展示，用户逐镜修改后点“保存全部”落盘。`script.json` 仍为唯一真源，本接口只产初稿、不落盘。

- 入参：`{name, total_seconds≥60, aspect, clip_seconds(4-12), style, source_text}`；原文超 12000 字截断前部并标注。
- prompt（`backend/breakdown.py`）：把每镜时长表明写进题面要求逐镜照抄；`video_mode` 全 `"text"`（v1 只产合法初稿）；台词与六段式提示词必须出自原文。
- 归一化保证：缺镜按计划补空镜、多镜截断；`start` 一律重算累加；末镜收敛使求和精确等于总时长；媒体字段丢弃。
- HTTPS 通道：Agnes 用池 Key（自动排除自建专用密钥）、自建用端点专用密钥，各自取用归还（`release` 计一次文本调用，429/401 按 `error_map.yaml` 分类）；opencode-zen 走本地 CLI（免登录，不碰密钥池）。文本未配/原文为空 400，模型返回非法 JSON 或校验失败 502（可重试或改短原文）。
- 前端：分解成功进分镜表（404 本地草稿提示与“连不上后端”区分）；离线保留“离线建空剧”搭架子。

## 4.11 渲染执行（worker 真跑，`backend/runner.py`）

此前 `retry` 只复位状态不干活；第一集实测又暴露“轮询 429 杀任务 + 桶空判死刑”——本节为修复后的约定。

- `POST /api/drama/{name}/start` 起后台线程（每剧防重入，在跑再点 409）；`GET /api/drama/{name}/render-status` 查是否在跑；`POST .../retry` 复位后自动只跑该镜。
- 同一 Key：提交 → 轮询到终态 → 归还，中途不放手；`VIDEO_LOCK` 全局串行；相邻提交 pacing 65s（覆盖 60s RPM 窗口）；轮询期间租约延长，防长任务被回收。
- 提交 429/超时：退避（60s×次数）重提同一镜，最多 3 次；其他错误直接失败。
- 轮询 429：按 `Retry-After`（缺省指数 5s 起、上限 60s）睡后继续问同一单，不丢任务、不毒化 Key，等待不计入轮询总超时（1800s）；轮询间隔默认 3s。
- 桶空但有活 Key：等 pacing 重试（最多 3 轮）；全作废/用光才停并明示。
- 断点续跑：产物存在 + `state done` 的阶段直接跳过，成功镜零重跑；单镜失败记 failed 继续下一镜，最后汇总；先拿 Key 再标 doing，状态不说谎。
- 合成门禁：成片只收分镜一一对应的视频——缺一镜（无文件/0 字节残留）就不合，不出残片版本、不碰指针，缺的镜标 failed 并点名；单镜重试补齐最后一块后自动出完整版，平时想出新版点“开始渲染”（全剧跑）。
- 可见性：提交成功先记 `video_id`；归还记分类标签；`render.log` 记等待/退避/分类。
- 约定：视频秒数取分镜时长四舍五入夹 4-12；无台词的镜跳过 TTS（记 done）；keyframe 缺首帧直接 failed（不烧配额）；整剧合成只在全剧开跑时做，单镜重试跳过合成。
- v1 无取消接口：开跑后等跑完或失败停；重复点击不会重复开工。
