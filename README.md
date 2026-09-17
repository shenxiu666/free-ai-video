# free-ai-video

零成本 AI 短剧客户端：在本地浏览器完成“题材 → 成片”。图片与视频走 Agnes 免费模型，TTS 默认免费方案，全链路单机可跑 60s 以上短剧。

- 前端：Vue3 SPA（Apple 质感：弹簧动效、半透明材质、深色模式、无障碍降级）
- 后端：本地 FastAPI（`localhost:8000`，REST + SSE），持有密钥池、模型适配、任务状态机与 FFmpeg 合成
- 前端不直调 Agnes、不存 Key 明文；只经 `localhost` 与后端通信

## 架构

```text
[Vue3 SPA] --REST/SSE--> [FastAPI localhost:8000]
 新建剧/分镜表/          KeyPool | Providers | Orchestrator
 渲染队列/密钥池+设置     TTS | FFmpeg | state.json 落盘
      |                        |
      +------> outputs/<剧名>/ <------+
```

端到端：`题材 → 大纲 → script.json（唯一真源）→ 图 → 视频 → 配音 → final.mp4`，`state.json` 断点续跑。

## 环境要求

- Node.js 18+（前端），Python 3.14（后端），FFmpeg（合成，`ffmpeg -version` 可用即可）
- Windows 用 PowerShell

## 克隆后补齐（首次一次）

以下东西故意不进 git（见根 `.gitignore`：密钥防泄漏、二进制/依赖体积大、产物本地生成），克隆后按表补齐即可：

| 没上传的东西 | 为什么 | 补齐命令（仓库根，PowerShell） |
|---|---|---|
| Python 依赖包 | `pip` 包不进仓库 | `pip install -r backend/requirements.txt` |
| `frontend/node_modules/` | `npm` 包不进仓库 | `npm install --prefix frontend` |
| `.env` | 含本机 Key 引用，绝不进 git | `Copy-Item .env.example .env`，再填 `AGNES_API_KEY`（见下节配置） |
| `tools/opencode/opencode.exe`（约 180MB） | 按机配给；**缺失不影响出片**，仅开发期 Agent 用 | `.\tools\opencode\install.ps1`（锁定版本见 `tools/opencode/version.txt`） |
| `outputs/<剧名>/` 历史成片 | 本地产物；后端运行时自动建目录 | 无需操作，跑一次渲染自动生成 |
| `frontend/dist/` | 构建产物；`npm run dev` 不需要它 | 如需生产构建再跑 `npm run build --prefix frontend` |

```powershell
git clone https://github.com/shenxiu666/free-ai-video.git
cd free-ai-video
pip install -r backend/requirements.txt
npm install --prefix frontend
Copy-Item .env.example .env
# 可选（缺失不影响出片）：.\tools\opencode\install.ps1
```

## 快速开始

双击仓库根的 `start.bat`（可右键发送到桌面快捷方式 / 固定到任务栏）：自动开后端（8000）+ 前端（5173）两个窗口并打开浏览器。窗口保持不闪退，出错直接看窗口里的报错。

手动方式（备用）：

```powershell
# 1) 后端
pip install -r backend/requirements.txt
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000

# 2) 前端（新终端，仓库根目录下）
npm install --prefix frontend
npm run dev --prefix frontend   # http://localhost:5173
```

打开前端 → `/keys` 设置页：点“一键激活”（首次必需，否则 Key 重启即丢）→ 录入 Agnes Key、配好文本模型（可一键拉取免费模型、点测试连通性）→ `/new` 粘贴小说/剧本原文，AI 自动分解分镜（60/90/120s 或自定义，下限 60s）→ `/shots` 检查修改分镜 → `/queue` 看进度拿成片（`outputs/<剧名>/final.mp4`）。

## 配置（三层覆盖：CLI > ENV > YAML）

`config/models.yaml` 放默认值，ENV 覆盖常用项（见 `.env.example`，复制为 `.env` 后填本机值，`.env` 不进 git）：

```powershell
Copy-Item .env.example .env
$env:AGNES_API_KEY = "sk-..."
```

| 变量 | 用途 |
|---|---|
| `AGNES_API_KEY(S)` | Agnes 鉴权（多 Key 逗号分隔，多免费 Key 默认不同账号、独立池） |
| `AGNES_TEXT_MODEL` | 文本模型（用户自填，如 `agnes/agnes-3.0-flash`；留空则任务被拒绝并指引配置） |
| `TTS_PROVIDER/TTS_VOICE` | 语音合成（默认 `edge-tts / zh-CN-XiaoxiaoNeural`） |
| `OPENCODE_MODE/OPENCODE_BIN` | opencode 二进制选择覆盖（见下） |

## 模型锁定

- 图片 `agnes-image-2.5-flash`：`size` 只用 `1K/2K/3K/4K` + `ratio`，`response_format` 只放 `extra_body` 内
- 视频 `agnes-video-2.5-flash`：`size` 固定字符串 `"720P"`，`seconds` 字符串 `"4"-"12"`，`mode` 仅 `text/keyframe/reference`
- 文本无主力、用户自配，`fallback` 默认 `manual`；产物标注 `generated_by`

## 内置 opencode

opencode 仅为开发期 Agent，运行时不强依赖。仓库在 `tools/opencode/` 内置一份二进制（锁定版本见 `version.txt`，当前 `1.18.31`；约 180MB，不进 git，新机按需配给）：

```powershell
.\tools\opencode\install.ps1
```

设置页（密钥池+设置 → opencode 调用）可切换 `内置（默认）/ 系统 PATH / 自定义路径 / 自动`，附带位置检测与可用性测试。详见 `docs/05-opencode-config.md §5.8`。

## 目录结构

```text
config/models.yaml      # 模型与参数默认值
config/opencode.yaml    # opencode 二进制选择
backend/                # FastAPI + KeyPool/Providers/Orchestrator/TTS/FFmpeg
frontend/               # Vue3 SPA（新建剧/分镜表/渲染队列/密钥池+设置）
tools/opencode/         # 内置 opencode（二进制按机配给，版本锁定）
outputs/<剧名>/         # script.json / state.json / images / videos / audio / final.mp4（最新）/ 成片/final_时间戳.mp4（每次保留）
docs/                   # 设计文档（下表）
```

## 文档索引

| 章 | 文档 | 内容 |
|---|---|---|
| 01 | `docs/01-overview-architecture.md` | 愿景/分层/模块/数据流/目录与 ADR |
| 02 | `docs/02-keypool-scheduling.md` | 独立池/状态机/RPM 桶/429 三级/24h 刷新/共享池探测 |
| 03 | `docs/03-encryption-activation.md` | KEK 派生/TPM+DPAPI/Env 规范/轮换/威胁模型 |
| 04 | `docs/04-drama-pipeline-tts.md` | script.json/60s+ 估算/一致性/TTS 矩阵/ffmpeg/重试 |
| 05 | `docs/05-opencode-config.md` | 双源注册/models/全开放配置/内置 opencode/drama-director 校验 |

## 验证

```powershell
python -m pytest backend/tests -q
npm run typecheck --prefix frontend
npm run build --prefix frontend
```

## 安全红线

- Key 明文、KEK、机器码明文永不落盘（仅 `AES-256-GCM` 密文落盘）、不进 Env/git/日志；前端只见掩码 `sk-前2~~~~后3`
- 换机拷贝 DB 必然解密失败，须重新激活后重新录入 Key
- `baseURL` 只到 `/v1`，`apiKey` 用 `{env:...}` 引用；`response_format` 禁顶层；不传 `tags:["img2img"]`
