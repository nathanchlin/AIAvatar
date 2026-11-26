# 一键启动 MiniMax H3 实时语音对话数字人（方案 A）
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$llamaExe = "H:\UnslothStudio\llama.cpp\build\bin\Release\llama-server.exe"
$modelPath = "H:\models\gemma-3-1b-it-Q4_K_M.gguf"

function Test-Url([string]$url) {
    try {
        $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 -NoProxy -ErrorAction Stop
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

# 1. 启动轻量本地对话 LLM (port 8080, 约 0.75GB 内存/显存，低延迟响应)
if (Test-Url "http://127.0.0.1:8080/health") {
    Write-Host "[OK] 本地 LLM 服务已在运行 (http://127.0.0.1:8080)" -ForegroundColor Green
} elseif ((Test-Path $llamaExe) -and (Test-Path $modelPath)) {
    Write-Host "[*] 启动本地轻量 LLM (gemma-3-1b-it, port 8080)..." -ForegroundColor Cyan
    Start-Process -FilePath $llamaExe `
        -ArgumentList @("-m", $modelPath, "-ngl", "0", "-c", "4096", "--port", "8080", "--host", "127.0.0.1") `
        -WindowStyle Minimized
    Start-Sleep -Seconds 3
} else {
    Write-Host "[!] 未找到本地 llama-server 或 gemma-3-1b 模型，将使用保底对话模式" -ForegroundColor Yellow
}

# 2. 启动数字人 Web 服务 (port 8765)
if (Test-Url "http://127.0.0.1:8765/api/status") {
    Write-Host "[OK] 数字人页面已在运行: http://127.0.0.1:8765" -ForegroundColor Green
} else {
    Write-Host "[*] 启动数字人 Web 服务: http://127.0.0.1:8765" -ForegroundColor Cyan
    Start-Process -FilePath "python" -ArgumentList @("$root\server.py") -WorkingDirectory $root -WindowStyle Minimized
    Start-Sleep -Seconds 2
}

Start-Process "http://127.0.0.1:8765"
