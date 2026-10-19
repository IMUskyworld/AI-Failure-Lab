# Local quality gate: run this before pushing.
# Usage:  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    $py = Join-Path $root ".venv/bin/python"
}
if (-not (Test-Path $py)) {
    Write-Host "no .venv found; run:  uv venv && uv pip install -e '.[dev]' (see DEVELOPMENT.md 22)" -ForegroundColor Red
    exit 2
}

function Invoke-Step($name, $argv) {
    Write-Host "`n=== $name" -ForegroundColor Cyan
    & $py @argv
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAILED: $name" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

Invoke-Step "ruff check" @("-m", "ruff", "check", "src", "tests", "scripts")
Invoke-Step "ruff format --check" @("-m", "ruff", "format", "--check", "src")
Invoke-Step "pyright (src)" @("-m", "pyright")
Invoke-Step "schemas are current" @("scripts/export_schemas.py", "--check")
Invoke-Step "pytest" @("-m", "pytest", "-q")
Invoke-Step "MVP acceptance audit (DEVELOPMENT.md 20)" @("scripts/verify_mvp.py", "--skip-tests")

Write-Host "`nall checks passed" -ForegroundColor Green
