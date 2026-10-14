@echo off
cd /d "%~dp0"
if not exist .env (
  copy .env.example .env >nul
  echo Created .env. Set Alpaca/FreeLLM keys and DASHBOARD_PASSWORD, then run again.
  exit /b 1
)
if "%~1"=="stop" (docker compose stop & exit /b)
if "%~1"=="logs" (docker compose logs -f & exit /b)
if "%~1"=="ps" (docker compose ps & exit /b)
docker compose up -d --build
