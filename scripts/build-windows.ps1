$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectDir

if (-not (Test-Path -LiteralPath ".venv")) {
    python -m venv .venv
}
& ".venv\Scripts\python.exe" -m pip install --upgrade pip
& ".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt
$env:PLAYWRIGHT_BROWSERS_PATH = "0"
& ".venv\Scripts\python.exe" -m playwright install chromium
& ".venv\Scripts\pyinstaller.exe" --noconfirm --clean CasePDFDownloader.spec

Write-Host ""
Write-Host "Windows package created in dist\Case PDF Downloader"
