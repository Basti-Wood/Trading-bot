@echo off
setlocal

set "CMD=%~1"
if "%CMD%"=="" set "CMD=all"

if not exist ".env" (
  echo ERROR: .env not found in current folder.
  exit /b 1
)

if /I "%CMD%"=="help" goto :help
if /I "%CMD%"=="build" goto :build
if /I "%CMD%"=="evolver" goto :evolver
if /I "%CMD%"=="bots" goto :bots
if /I "%CMD%"=="dashboard" goto :dashboard
if /I "%CMD%"=="all" goto :all
if /I "%CMD%"=="up" goto :all
if /I "%CMD%"=="logs" goto :logs
if /I "%CMD%"=="ps" goto :ps
if /I "%CMD%"=="restart" goto :restart
if /I "%CMD%"=="stop" goto :stop
if /I "%CMD%"=="down" goto :down
if /I "%CMD%"=="clean" goto :clean

echo Unknown command: %CMD%
goto :help

:help
echo Trading Bot Commands
echo   start.bat build      - Build all images
echo   start.bat evolver    - Start evolver only
echo   start.bat bots       - Start all trading bots
echo   start.bat dashboard  - Start dashboard only
echo   start.bat all        - Start evolver + bots + dashboard
echo   start.bat logs       - Follow all logs
echo   start.bat ps         - Show running services
echo   start.bat restart    - Restart all services
echo   start.bat stop       - Stop containers
echo   start.bat down       - Stop and remove containers
echo   start.bat clean      - Down plus remove volumes
exit /b 0

:build
docker compose build
exit /b %errorlevel%

:evolver
docker compose up -d evolver
exit /b %errorlevel%

:bots
docker compose up -d bot_funds bot_etf bot_stocks bot_crypto bot_politician
exit /b %errorlevel%

:dashboard
docker compose up -d dashboard
exit /b %errorlevel%

:all
docker compose up -d evolver bot_funds bot_etf bot_stocks bot_crypto bot_politician dashboard
exit /b %errorlevel%

:logs
docker compose logs -f
exit /b %errorlevel%

:ps
docker compose ps
exit /b %errorlevel%

:restart
docker compose restart evolver bot_funds bot_etf bot_stocks bot_crypto bot_politician dashboard
exit /b %errorlevel%

:stop
docker compose stop
exit /b %errorlevel%

:down
docker compose down
exit /b %errorlevel%

:clean
docker compose down -v
exit /b %errorlevel%
