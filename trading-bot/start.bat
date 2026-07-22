@echo off
setlocal

set "CMD=%~1"

rem ============================================================================
rem Default (no args): build everything and launch detached
rem ============================================================================
if "%CMD%"=="" (
    call :check_env || exit /b 1
    echo.
    echo [1/3] Building base image ^(trading-bot-base^)...
    docker build -f Dockerfile.base -t trading-bot-base . || exit /b 1
    echo.
    echo [2/3] Building all services...
    docker compose build || exit /b 1
    echo.
    echo [3/3] Starting all services detached...
    docker compose up -d evolver bot_funds bot_etf bot_stocks bot_crypto bot_politician dashboard
    if %errorlevel% neq 0 exit /b %errorlevel%
    echo.
    echo All services running. Dashboard at http://localhost:1337
    echo Use 'start.bat ps' to see status, 'start.bat logs' to follow logs.
    exit /b 0
)

rem ============================================================================
rem Commands
rem ============================================================================
if /I "%CMD%"=="help"    goto :help
if /I "%CMD%"=="ps"      goto :ps
if /I "%CMD%"=="logs"    goto :logs
if /I "%CMD%"=="stop"    goto :stop
if /I "%CMD%"=="down"    goto :down
if /I "%CMD%"=="restart" goto :restart
if /I "%CMD%"=="clean"   goto :clean
if /I "%CMD%"=="fclean"  goto :fclean

echo Unknown command: %CMD%
goto :help

rem ---------------------------------------------------------------------------

:check_env
if not exist ".env" (
    echo ERROR: .env not found. Copy .env.example to .env and fill in your keys.
    exit /b 1
)
exit /b 0

:help
echo.
echo Trading Bot — start.bat
echo.
echo   start.bat              Build everything ^& start detached ^(default^)
echo   start.bat ps           Show running services
echo   start.bat logs         Follow all logs
echo   start.bat restart      Restart all services
echo   start.bat stop         Stop containers
echo   start.bat down         Stop ^& remove containers
echo   start.bat clean        Down + remove volumes
echo   start.bat fclean       Full clean: down, remove volumes ^& base image
echo.
exit /b 0

:ps
docker compose ps
exit /b %errorlevel%

:logs
docker compose logs -f
exit /b %errorlevel%

:stop
docker compose stop
exit /b %errorlevel%

:down
docker compose down
exit /b %errorlevel%

:restart
docker compose restart evolver bot_funds bot_etf bot_stocks bot_crypto bot_politician dashboard
exit /b %errorlevel%

:clean
docker compose down -v
exit /b %errorlevel%

:fclean
echo Removing containers, volumes, AND base image...
docker compose down -v
docker rmi trading-bot-base 2>nul
echo Done.
exit /b 0
