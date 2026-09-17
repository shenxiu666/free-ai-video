# 按 version.txt 配给内置 opencode：npm 全局安装同版本后複製 exe 到本目录并校验。
param([string]$Version = "")

$ErrorActionPreference = 'Stop'
$dir = Split-Path $MyInvocation.MyCommand.Definition -Parent
if (-not $Version) {
  $Version = (Get-Content -LiteralPath (Join-Path $dir 'version.txt') -TotalCount 1).Trim()
}
if (-not $Version) { throw 'version.txt 为空，无法确定锁定版本' }

Write-Host "target version: $Version"
npm install -g "opencode-ai@$Version"

$candidates = @(
  (Join-Path $env:APPDATA 'npm\node_modules\opencode-ai\bin\opencode.exe'),
  (Join-Path $env:ProgramFiles 'nodejs\node_modules\opencode-ai\bin\opencode.exe')
)
$src = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $src) { throw '未找到 npm 全局 opencode.exe（检查 npm prefix）' }

$dst = Join-Path $dir 'opencode.exe'
Copy-Item -LiteralPath $src -Destination $dst -Force
$got = & $dst --version 2>&1 | Select-Object -First 1
Write-Host "installed: $dst"
Write-Host "version: $got"
if ("$got".Trim() -ne $Version) {
  Write-Warning "版本与锁定不一致（期望 $Version，实际 $got），请检查 npm 缓存/源"
}
