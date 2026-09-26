@echo off
rem Stops the Jellyfin SyncPlay recorder (Python).
cd /d "%~dp0"

if not exist "telemetry-py\recorder.pid" (
    echo Recorder is not running.
    pause
    exit /b
)

set /p RPID=<"telemetry-py\recorder.pid"
taskkill /F /PID %RPID% >nul 2>nul
del "telemetry-py\recorder.pid" >nul 2>nul
echo Recorder stopped.
pause
