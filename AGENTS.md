<!-- CODEGRAPH_START -->
## CodeGraph

In repositories indexed by CodeGraph (a `.codegraph/` directory exists at the repo root), reach for it BEFORE grep/find or reading files when you need to understand or locate code:

- **MCP tool** (when available): `codegraph_explore` answers most code questions in one call — the relevant symbols' verbatim source plus the call paths between them, including dynamic-dispatch hops grep can't follow. Name a file or symbol in the query to read its current line-numbered source. If it's listed but deferred, load it by name via tool search.
- **Shell** (always works): `codegraph explore "<symbol names or question>"` prints the same output.

If there is no `.codegraph/` directory, skip CodeGraph entirely — indexing is the user's decision.
<!-- CODEGRAPH_END -->

## Windows约束 
当前环境是 Windows 11 / pwsh7 
- 默认禁止使用 Bash 语法，除非确定此shell处在 Linux 环境 
- 不要使用 Bash 引号/转义习惯，在 PowerShell 命令里，复杂正则优先用单引号包裹。 
- 如果正则本身同时包含单引号和双引号，优先拆成多个简单 rg 命令。 
- 执行多行 Python 禁止使用 Bash heredoc；改用 PowerShell here-string | python - 
- pwsh 中，语句块表达式（如 `foreach`、`if`）不能直接作为管道输入。 需要先使用 `$()` / `@()` 包裹，或先赋值给变量。 普通命令输出可直接进入管道，无需额外包裹。 
- PowerShell 使用 `rg` 时，通配目录必须先用 `Get-ChildItem -Filter` 展开为真实路径，禁止直接把含 `*` 的搜索路径传给 `rg`。

## 项目说明 free-ai-video
零成本 AI 短剧客户端：Vue3 SPA + 本地 FastAPI（localhost REST+SSE），以后再考虑服务端。opencode CLI 仅为开发期 Agent，运行时不强依赖。
详细设计文档见 `docs/`：`README.md` 为索引，`01` 总览架构、`02` 多Key池调度、`03` 加密激活、`04` 短剧流水线与TTS、`05` opencode与模型配置。改代码前先读对应章节。

- 模型锁定：图片 `agnes-image-2.5-flash`（`POST /v1/images/generations`，`size` 只用 `1K/2K/3K/4K` + `ratio`，`extra_body.response_format` 禁顶层）；视频 `agnes-video-2.5-flash`（`POST /v1/videos` + `GET /agnesapi?video_id=&model_name=`，`size` 固定字符串 `"720P"`，`seconds` 字符串 `"4"-"12"`，`mode` 仅 `text/keyframe/reference`）。旧名 `2.1-flash / v2.0` 一律视为过时。
- 文本无主力、用户自配：`text.provider/model` 默认留空，留空拒绝任务并指引；`fallback` 默认 `manual`，仅用户显式配置才降级；产物标注 `generated_by`。
- 全开放自定义：`config/models.yaml + ENV + CLI` 三层覆盖（优先级 `CLI > ENV > YAML`），`opencode.json` 注册 `agnes + opencode Zen` 双源，`/models` 须同时列出两家。
- 多免费 Key 默认不同账号、独立池；同账号同类型共享池不叠加、不提速。状态 `2空闲/1使用中(+10min租约)/0用光` + 冷却态；429 按 `error_map.yaml` 分 A 瞬时冷却 / B 配额用光 / C 作废；24h 滚动只用于日配额，RPM 用分钟令牌桶（免费参考：视频1/文本20/图1K 20，可能调整）。
- 加密：`KEK=PBKDF2(后端长期派生密钥||TPM密封码||salt)`，`AES-256-GCM` 落盘 `%APPDATA%/free-ai-video/keys_encrypted.db`，纯离线；无 TPM 用 DPAPI（`CurrentUser`）兜底。KEK/API明文/机器码明文绝不进 Env、git、日志；前端只见掩码 `sk-前2~~~~后3`。
- 短剧下限 60s（60/90/120s/自定义），`script.json` 为唯一真源，`state.json` 断点续跑；一致性用 `character_refs≤5` + 相邻镜 keyframe 链；TTS 默认 `edge-tts`，备选按 `04` 章 manual 切换；合成 `ffmpeg concat+xfade+loudnorm+字幕`。
- 安全红线：不明文落盘、不跨机拷贝 DB、不在顶层放 `response_format`、不传 `tags:["img2img"]`、`baseURL` 只到 `/v1`、`apiKey` 用 `{env:...}` 引用。

## 协作规范 Subagents
- 允许按模块拆分并行：每个模块一个 builder subagent（`Task: general`），任务提示必须自包含（目标文件、锁定约束、验收标准、只动所属模块）。
- 强制交叉 review：每个 builder 产出必须再派一个 reviewer subagent（`Task: general`，与 builder 不同会话）做独立审查，输出 `通过 / 需返工（列出文件:行号+原因）`；返工循环直到 reviewer 通过。
- 主 agent 只做拆分、落盘、跑验证（build/test/lint），不与 subagent 同写同一文件；reviewer 发现跨模块冲突时由主 agent 裁决。
- 每次修改都在`docs/mem`中生成修改说明
