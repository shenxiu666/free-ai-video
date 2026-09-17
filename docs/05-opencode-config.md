# 05 opencode与模型配置

> 本章模型名已按锁定版本修正：`agnes-image-2.5-flash` / `agnes-video-2.5-flash` / 文本用户自配（含 `agnes-3.0-flash` 选项）。旧文档中的 `2.1-flash / v2.0` 一律视为过时。

## 5.1 设计目标与双源策略

文本可走 Agnes 或 opencode Zen 免费通道，图片/视频只能走 Agnes（opencode 免费全是文本模型）。`opencode.json` 同时注册两家，`/models` 必须同时列出两家。Zen 为限时免费的 OpenAI-compatible 文本通道（`/v1/chat/completions`），可用 `opencode/big-pickle`、`opencode/mimo-v2.5-free`、`opencode/deepseek-v4-flash-free`、`opencode/nemotron-3-ultra-free` 等；另支持 Models.dev 的 75+ provider，经 `/connect` 接入后用 `/variants` 选变体。

文本坚持“无主力、用户自配”：`text.provider/model` 默认留空，留空时任务直接拒绝并指引配置，不隐式猜模型。`fallback` 仅用户显式配置才生效，默认 `manual` 不自动降级。所有生成物输出均标注 `generated_by`（provider+model+时间），便于审计与复现。

## 5.2 opencode.json 示例

Agnes 用 `@ai-sdk/openai-compatible` 注册，`baseURL` 只到 `/v1`，`apiKey` 引用 `{env:AGNES_API_KEY}`；Zen 用 `opencode auth login --provider zen` 登录，不在 JSON 里写死 key。

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "agnes": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Agnes AI Hub",
      "options": {
        "baseURL": "https://apihub.agnes-ai.com/v1",
        "apiKey": "{env:AGNES_API_KEY}"
      },
      "models": {
        "agnes-3.0-flash": { "name": "agnes-3.0-flash（文本，可选）" },
        "agnes-2.5-flash": { "name": "agnes-2.5-flash（文本备选）" }
      }
    }
  }
}
```

图片/视频不经过 opencode chat 模型通道，由后端直调：

- 图片：`POST https://apihub.agnes-ai.com/v1/images/generations`，`model=agnes-image-2.5-flash`
- 视频：`POST https://apihub.agnes-ai.com/v1/videos`，`model=agnes-video-2.5-flash`

命令与 Agent 均可用 `provider/model` 全称或 `--model` 切换，例如 `opencode run --model opencode/mimo-v2.5-free "改写台词"`、`--model agnes/agnes-3.0-flash`。Agent frontmatter 中声明 `model:` 即覆写默认模型。

## 5.3 config/models.yaml 示例

三层覆盖优先级：`CLI 参数 > ENV > config/models.yaml`，全开放可改。

```yaml
text:
  provider: ""   # 留空：拒绝任务并指引配 Zen/Agnes/自建（下拉备选，也兼容误填 URL 自动识别为端点）
  model: ""      # 如 agnes/agnes-3.0-flash 或 opencode/mimo-v2.5-free（可点“拉取免费模型”选择）
  protocol: openai  # 接口协议：openai（OpenAI 兼容 /v1/chat/completions，三家均用此）
  base_url: ""   # 接口端点：留空用提供商默认；自建填如 http://127.0.0.1:11434/v1
  temperature: 0.7   # 采样温度 0-2
  max_tokens: 8192   # 单次最大 Token 数（AI 分镜输出长 JSON，建议 ≥8192）
  timeout_s: 120     # 请求超时秒
  fallback: { mode: manual }  # 显式配才降级
image:
  provider: agnes
  model: "agnes-image-2.5-flash"
  size: "1K"     # 只允许 1K/2K/3K/4K
  ratio: "9:16"  # 1:1/3:4/4:3/16:9/9:16/2:3/3:2/21:9
video:
  provider: agnes
  model: "agnes-video-2.5-flash"
  size: "720P"   # 只允许字符串 720P
  mode: "reference"  # text/keyframe/reference
  seconds: "8"   # 字符串 4-12
  aspect_ratio: "9:16"
tts:
  provider: "edge-tts"
  voice: "zh-CN-XiaoxiaoNeural"
```

## 5.4 ENV 表

| 变量 | 用途 | 示例 |
|---|---|---|
| `AGNES_API_KEY` | Agnes 唯一鉴权（多 Key 用 `AGNES_API_KEYS` 逗号分隔，多账号） | `sk-...`，pwsh 用 `$env:AGNES_API_KEY="sk-..."` |
| `AGNES_TEXT_MODEL` | 文本模型（用户自填） | `agnes/agnes-3.0-flash` 或 `opencode/mimo-v2.5-free` |
| `FREEAI_TEXT_PROVIDER/PROTOCOL/BASE_URL` | 文本提供商/协议/端点 | `agnes / openai / http://127.0.0.1:11434/v1` |
| `FREEAI_TEXT_TEMPERATURE/MAX_TOKENS/TIMEOUT_S` | 文本采样温度/最大 Token/超时秒 | `0.7 / 8192 / 120` |
| `AGNES_IMAGE_MODEL` | 图片模型 | `agnes-image-2.5-flash` |
| `AGNES_VIDEO_MODEL` | 视频模型 | `agnes-video-2.5-flash` |
| `TTS_PROVIDER/TTS_VOICE` | 语音合成 | `edge-tts / zh-CN-XiaoxiaoNeural` |

Windows pwsh 设置示例：`$env:AGNES_API_KEY="sk-..."; opencode /models`。

## 5.5 切换用法：/models /connect --model

`/models` 查看 agnes+zen 是否同时在线；`/connect` 接 Models.dev 第三方源；`/variants` 切换同一模型的不同变体；`--model provider/model` 单次覆写；Agent 文件头 `model:` 持久覆写。调试时先 `/models` 确认，再用 `--model` 最小复现。

## 5.6 drama-director 指令与校验清单

`.opencode/agent/drama-director.md` 为短剧总导演，编排 `/drama-new`（建剧）、`/drama-shot`（分镜生图）、`/drama-render`（图生视频/合成）。指令把图文视频硬约束变为前置校验：

- 图片：`size` 必须命中 `1K/2K/3K/4K`，`ratio` 合法，`extra_body.response_format` 放 `extra_body` 内，不拼 Bearer，不传 `tags`；
- 视频：`size="720P"` 字符串，`mode` 仅 `text/keyframe/reference`，`seconds` 传字符串，`reference` 检查 `images≤5/audios≤3` 且禁 `videos`，提交后取 `video_id` 并用 `model_name` 轮询再下载。

任一失败即阻断并提示修正，不进入计费调用。

## 5.7 Windows 排错与验收

常见错：手动加 `Bearer ` 前缀（SDK 已加，重复即 401）；`baseURL` 写到 `/v1/chat/completions`（只到 `/v1`）；Zen 未 `auth login --provider zen` 导致列表无 Zen；`seconds` 传数字被拒。验收：`/models` 同时见 agnes 与 zen；text 留空时任务拒绝有指引；显式配 fallback 才降级；产物含 `generated_by`。

## 5.8 内置 opencode 二进制（调用位置可选）

opencode 仅为开发期 Agent（代码生成/调试），运行时不强依赖，缺失不影响出片。但为保证“在哪都能调到同一个 opencode”，仓库内置一份二进制，设置页可切换。

| 项 | 值 |
|---|---|
| 来源 | `npm i -g opencode-ai@1.18.31`（MIT），从本机实装複製 |
| 锁定版本 | `tools/opencode/version.txt`（当前 `1.18.31`） |
| 内置路径（默认首选） | `tools/opencode/opencode.exe` |
| 配给脚本 | `tools/opencode/install.ps1`（按 `version.txt` 重装複製并校验 `--version`） |

二进制约 180MB，**不进 git**（根 `.gitignore` 忽略）：每台机器首次用 `install.ps1` 配给；`version.txt` 进 git，版本不一致时设置页与 `/models` 会提示 `pinned_match: false`。

### 选择优先级

`请求级 bin_path > ENV(OPENCODE_BIN / OPENCODE_MODE) > config/opencode.yaml`，与三层覆盖同精神（`CLI > ENV > YAML`）。

`config/opencode.yaml`（无秘密值，可进 git，默认 `builtin`）：

```yaml
mode: builtin   # builtin | system | custom | auto
bin_path: ""    # custom 模式必填：opencode 可执行文件绝对路径
```

- `builtin`（默认）：只用 `tools/opencode/opencode.exe`。
- `system`：只用系统 PATH 中的 `opencode`。
- `custom`：只用 `bin_path`（不存在则拒绝保存并 400）。
- `auto`：内置存在用内置，否则回退系统 PATH。
- `OPENCODE_BIN` 非空时覆盖任何模式；`OPENCODE_MODE` 覆盖文件中的 `mode`（见 `.env.example`）。

### 位置检测顺序

后端 `backend/opencode_manager.py` 的 `detect_candidates()` 按序去重：`builtin → env → path → npm-global（真实 exe，绕过 npm 垫片）→ user（%USERPROFILE%/.opencode/bin）→ custom`。存在性检测很快；版本探测（跑 `<bin> --version`，npm 垫片经解释器调用、一律 `shell=False`）走单独接口以免拖慢状态页。前端设置页“检测全部位置”调全量探测，“测试可用性”只跑当前生效项。

### 后端接口（`backend/main.py`，CORS 仅 localhost）

| 接口 | 说明 |
|---|---|
| `GET /api/opencode/status` | 选择 + 生效项（path/source/版本/存在性/与锁定是否一致）+ 锁定版本 + 候选列表 |
| `POST /api/opencode/detect` | 全量版本探测（每项限时，慢项记 `error` 不阻塞） |
| `POST /api/opencode/select` | 保存 `{mode, bin_path}` 到 `config/opencode.yaml`（custom 校验存在性） |
| `POST /api/opencode/test` | 跑 `<bin> --version`（缺省测当前生效项），返回 `{ok, version, latency_ms}` |
| `GET /models` | `opencode.cli` 段追加当前生效二进制信息（失败不抛，`version: null`） |

### 前端设置页（密钥池+设置 → opencode 调用）

单选 `内置（默认）/ 系统 PATH / 自定义路径 / 自动` + 自定义路径输入框 + `保存选择 / 检测全部位置 / 测试可用性` 三按钮 + 候选列表（来源/存在徽/版本/路径）+ 当前生效行与版本一致徽。状态进 `localStorage` 做离线兜底（路径非秘密值）。Apple 风格沿用本页现有卡片/按钮/深色模式/减弱动效。

### 验收

- [ ] `GET /api/opencode/status` 生效项为内置且 `version == version.txt`（`pinned_match: true`）
- [ ] 设置页切换 system/custom/auto 后生效项跟随变化；custom 填不存在路径被 400 拒绝
- [ ] 内置删除后 status 显示缺失且 `auto` 回退到 PATH，`builtin` 模式测试报不可用并指引 `install.ps1`
- [ ] `/models` 的 `opencode.cli` 与设置页一致；`config/opencode.yaml` 可进 git 且无秘密值

## 5.9 文本连通性测试与免费模型拉取

设置页文本节新增两个动作，均走后端、密钥经密钥池取用归还（明文不出内存）：

- `POST /api/text/test`（“测试连通性”）：用当前表单值（未保存也可测）真实 ping 一次文本通道，发一条极短消息，返回延迟与模型原样回复。401/403/429 分别给出查 Key、等冷却的可操作提示。
- `GET /api/text/status`（通道状态）：当前提供商/模型 + 密钥来源（下表）+ 自建密钥是否已配。
- `POST /api/text/custom-key`（“保存端点密钥”）：自建端点专用密钥进加密密钥池（备注“自建端点”），不存配置文件，返回掩码。
- `GET /api/opencode/models?provider=opencode`（“拉取免费模型”）：跑内置（或选中）二进制的 `opencode models [--verbose]`，按 `cost` 全 0 判定免费并展示名称/上下文长度；二进制缺失或执行失败自动回内置候选，保证页面可用。选中某模型后自动填入提供商/模型/端点（`opencode/*` 系走 Zen 通道）。

Zen 通道说明（已实测）：`opencode/*` 免费模型**免登录**，走本地 opencode CLI（`run --model provider/model --format json`，取 text 事件正文），不直调 Zen HTTPS、不需要 token、不碰密钥池。调用时自动追加“不要调用任何工具”护栏，并在空 scratch 目录执行；`temperature/max_tokens` 对 CLI 通道无效（用模型默认）。`--format json` 的 token 统计仅记录不计费。每次调用会在 opencode 本地留一条 session 记录（`opencode session` 可见），属正常现象。

密钥归属（顶层 Key 只给 Agnes 用，三者绝不串用）：

| 提供商 | 密钥来源 | 无密钥时 |
|---|---|---|
| `agnes` | 上方密钥池（自动排除“自建端点”专用密钥） | 400 指引去录 Key |
| `opencode-zen`（`opencode/*` 免费模型） | 本地 CLI（免登录，不碰池） | 二进制缺失才报配给指引 |
| `openai-compatible`（自建） | 文本节“端点密钥”（专用，进加密池） | 400 指引填写端点密钥，不回退用 Agnes Key |

验收：拉取后 `opencode/mimo-v2.5-free` 等在列表中且标免费；选中后提供商自动切为 opencode Zen、端点留空；未登录 Zen 时连通性测试照样通过（CLI 通道免登录）。
