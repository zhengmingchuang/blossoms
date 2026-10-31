# DocFusion - one-click backend launcher for Windows
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\start_dev.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\start_dev.ps1 -Port 8000 -WithFrontend
#
# What it does:
#   1. create backend/.venv if missing
#   2. install backend/requirements.txt if fastapi is not importable
#   3. create backend/.env from .env.example if missing
#   4. start uvicorn (and optionally the Vite dev server)
#
# NOTE: messages are ASCII on purpose, Windows PowerShell 5.1 mangles UTF-8
#       script files without BOM.

param(
    [int]$Port = 8000,
    [switch]$WithFrontend,
    [switch]$SkipInstall
)

$ErrorActionPreference = 'Stop'

$root    = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'backend'
$frontend = Join-Path $root 'frontend'
$venv    = Join-Path $backend '.venv'
$py      = Join-Path $venv 'Scripts\python.exe'

if (-not (Test-Path $backend)) { throw "backend directory not found: $backend" }

# ---- 1. venv ----
if (-not (Test-Path $py)) {
    Write-Host "[1/4] creating venv -> $venv"
    $basePy = (Get-Command python -ErrorAction SilentlyContinue)
    if (-not $basePy) { throw "python not found in PATH. Install Python 3.11+ first." }
    & python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "failed to create venv" }
} else {
    Write-Host "[1/4] venv exists"
}

# ---- 2. dependencies ----
if ($SkipInstall) {
    Write-Host "[2/4] skip install (-SkipInstall)"
} else {
    & $py -c "import fastapi" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[2/4] installing requirements (first run may take a few minutes)"
        & $py -m pip install --upgrade pip | Out-Null
        & $py -m pip install -r (Join-Path $backend 'requirements.txt')
        if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
    } else {
        Write-Host "[2/4] dependencies already installed"
    }
}

# ---- 3. .env ----
$envFile = Join-Path $backend '.env'
$envExample = Join-Path $backend '.env.example'
if (-not (Test-Path $envFile)) {
    Copy-Item $envExample $envFile
    Write-Host "[3/4] created backend/.env from .env.example"
    Write-Host "      >>> fill in LLM_API_KEY before using LLM features <<<"
} else {
    Write-Host "[3/4] backend/.env exists"
}

# ---- 4. start ----
$distIndex = Join-Path $frontend 'dist\index.html'
if (Test-Path $distIndex) {
    Write-Host "[4/4] frontend/dist found - single port mode: http://127.0.0.1:$Port"
} else {
    Write-Host "[4/4] frontend/dist missing - API only. Run 'npm run build' in frontend for single port mode."
}

if ($WithFrontend) {
    if (Test-Path (Join-Path $frontend 'package.json')) {
        Write-Host "      starting Vite dev server on :3000 ..."
        Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', 'npm run dev' -WorkingDirectory $frontend
    } else {
        Write-Warning "frontend/package.json not found, skip dev server"
    }
}

Write-Host ""
Write-Host "Docs: README.md    Health: http://127.0.0.1:$Port/api/health"
Write-Host ""

Push-Location $backend
try {
    & $py -m uvicorn main:app --host 0.0.0.0 --port $Port
} finally {
    Pop-Location
}
