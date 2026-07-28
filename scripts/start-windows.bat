@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Case PDF Downloader is not set up yet.
  echo Run scripts\setup-windows.ps1 from PowerShell first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" run.py
