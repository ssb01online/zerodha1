@echo off
REM Start Kite session tray (token green/red). Invoked at logon by Task Scheduler.
setlocal
cd /d "%~dp0.."
uv run sessiontray
exit /b %ERRORLEVEL%
