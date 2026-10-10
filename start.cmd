@echo off
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 (
  echo uv is not on PATH. Install it from https://docs.astral.sh/uv/ then run this again.
  pause
  exit /b 1
)
uv run python start.py %*
if errorlevel 1 pause
