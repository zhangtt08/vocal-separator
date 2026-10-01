@echo off
setlocal

title Vocal Separator Launcher
set "APP_DIR=%~dp0"
set "BACKEND_DIR=%APP_DIR%backend"
set "APP_URL=http://127.0.0.1:3000"
set "BACKEND_URL=http://127.0.0.1:8000/api/health"

echo ========================================
echo   Vocal Separator - Local AI Service
echo ========================================
echo.

if not exist "%APP_DIR%package.json" goto missing_files
if not exist "%BACKEND_DIR%\main.py" goto missing_files

where python >nul 2>&1
if errorlevel 1 goto missing_python

where npm.cmd >nul 2>&1
if errorlevel 1 goto missing_node

echo [1/4] Checking Python backend...
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
if not errorlevel 1 goto backend_running
start "Vocal Separator Backend - Keep Open" /D "%BACKEND_DIR%" cmd.exe /k python main.py
goto frontend_check

:backend_running
echo       Backend is already running.

:frontend_check
echo [2/4] Checking web frontend...
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
if not errorlevel 1 goto frontend_running
start "Vocal Separator Frontend - Keep Open" /D "%APP_DIR%" cmd.exe /k npm.cmd run dev
goto wait_services

:frontend_running
echo       Frontend is already running.

:wait_services
echo [3/4] Waiting for services. First launch may be slower...
rem Polling happens inside one PowerShell process (scripts\wait-for-services.ps1)
rem instead of one process per second from batch.
set "PS=%WINDIR%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS%" set "PS=powershell"
"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\wait-for-services.ps1" -FrontendUrl "%APP_URL%" -BackendUrl "%BACKEND_URL%" -TimeoutSec 45
if not errorlevel 1 goto ready
goto startup_failed

:ready
echo [4/4] Ready. Opening the Chinese interface...
start "" "%APP_URL%"
echo.
echo ========================================
echo   URL: %APP_URL%
echo   Keep the frontend and backend windows open.
echo ========================================
echo.
pause
exit /b 0

:missing_files
echo [ERROR] App files are incomplete. Keep start.bat in the app folder.
goto failed

:missing_python
echo [ERROR] Python was not found in PATH.
goto failed

:missing_node
echo [ERROR] Node.js and npm were not found in PATH.
goto failed

:startup_failed
echo [ERROR] Services were not ready within 45 seconds.
echo Check the frontend and backend windows for the error message.

:failed
echo.
pause
exit /b 1
