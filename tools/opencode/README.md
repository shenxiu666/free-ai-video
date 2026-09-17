# 内置 opencode（开发期 Agent 二进制）

> opencode 仅为开发期 Agent（代码生成/调试），运行时不强依赖，缺失不影响出片（见 `docs/01` 非目标）。

- 来源：`npm i -g opencode-ai@1.18.31`（MIT），本机实装複製。
- 锁定版本：见同目录 `version.txt`（当前 `1.18.31`）。
- 内置路径（默认首选）：`tools/opencode/opencode.exe`。
- 二进制约 180MB，**不进 git**（见根 `.gitignore`）：每台机器用 `install.ps1` 按 `version.txt` 配给，版本不一致时设置页会提示。

```powershell
# 本机配给/升级到锁定版本
.\tools\opencode\install.ps1
# 指定版本
.\tools\opencode\install.ps1 -Version 1.18.31
```

选择逻辑（后端 `backend/opencode_manager.py`，文档 `docs/05 §5.8`）：
`自定义路径 > ENV(OPENCODE_BIN) > 设置页模式(默认 builtin) > auto 回退（内置→系统 PATH）`。
