@echo off
REM NFO EOD after download_ready (15:40 + 30m = 16:10 IST). Clock assumed IST.
setlocal
cd /d "%~dp0.."
if not exist "data\ohlcv\_eod" mkdir "data\ohlcv\_eod"
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set STAMP=%%I
uv run ohlcv eod --markets nfo --no-wait >> "data\ohlcv\_eod\nfo-%STAMP%.log" 2>&1
exit /b %ERRORLEVEL%
