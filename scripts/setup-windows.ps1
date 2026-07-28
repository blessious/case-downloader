$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectDir

if (-not (Test-Path -LiteralPath ".venv")) {
    python -m venv .venv
}
& ".venv\Scripts\python.exe" -m pip install --upgrade pip
& ".venv\Scripts\python.exe" -m pip install -r requirements.txt
& ".venv\Scripts\python.exe" -m playwright install chromium

Write-Host ""
Write-Host "Setup complete. Run scripts\start-windows.bat to open Case PDF Downloader."
