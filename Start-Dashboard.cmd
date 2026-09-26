@echo off
rem Jellyfin SyncPlay Doctor launcher: opens the running dashboard, or starts the
rem recorder (single instance, auto port).
cd /d "%~dp0"
where python >nul 2>nul || (echo Python 3 not found on PATH & pause & exit /b 1)
python launch.py
