# 前端构建脚本（任务书 §4.5）：Windows PowerShell 版，与 build_frontend.sh 步骤一致。
#
# 为什么 NEXT_TELEMETRY_DISABLED=1：本项目不向 Vercel 上报使用数据（刻意选择，
# 详见 frontend/README.md），写在脚本里而不是 .env.local，是为了"别人直接跑
# npx next build"时也不会漏掉——脚本是唯一的正式入口。
#
# 任一步失败即退出：半成品 out/ 挂载进生产比构建失败更难排查。
$ErrorActionPreference = "Stop"
$env:NEXT_TELEMETRY_DISABLED = "1"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$FrontendDir = Join-Path $ProjectRoot "frontend"

function Invoke-Step {
    param([string]$Name, [scriptblock]$Body)
    Write-Host "== $Name =="
    & $Body
    if ($LASTEXITCODE -ne 0) {
        Write-Host "$Name 失败（退出码 $LASTEXITCODE），已中止" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

Write-Host "== 1/5 工具链检查 =="
foreach ($tool in @("node", "npm")) {
    if (null -eq (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Write-Host "缺少 $tool，请先安装 Node 24（自带 npm 12）" -ForegroundColor Red
        exit 1
    }
}
Write-Host "node: $(& node --version)"
Write-Host "npm : $(& npm --version)"

Set-Location $FrontendDir

Invoke-Step "2/5 安装依赖（npm ci）" {
    # 刻意用 npm ci 而不是 npm install：锁文件与 package.json 不一致时必须失败，
    # 而不是静默改写 package-lock.json（静默改锁文件是协作地狱的开始，任务书 §4.5）。
    & npm ci
}

Invoke-Step "3/5 代码检查（eslint + tsc）" {
    & npm run lint
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & npx tsc --noEmit
}

Invoke-Step "4/5 静态导出（next build）" {
    & npm run build
}

Write-Host "== 5/5 产物校验 =="
$IndexHtml = Join-Path $FrontendDir "out\index.html"
if (-not (Test-Path $IndexHtml)) {
    Write-Host "构建未产出 out\index.html，挂载点会退化（app.py:mount_frontend 只 WARNING）" -ForegroundColor Red
    exit 1
}
$Artifacts = Get-ChildItem -Path (Join-Path $FrontendDir "out") -Recurse -File
$SizeMb = [math]::Round(($Artifacts | Measure-Object -Property Length -Sum).Sum / 1MB, 2)
Write-Host "产物：frontend\out —— $($Artifacts.Count) 个文件，合计 $SizeMb MB"
Write-Host "out\login\index.html 存在：$(Test-Path (Join-Path $FrontendDir 'out\login\index.html'))"

Write-Host ""
Write-Host "下一步（生产形态，根目录执行）："
Write-Host "  uv run uvicorn sequoia_x.api.app:app --port 8000"
Write-Host "然后浏览器访问 http://127.0.0.1:8000/"
