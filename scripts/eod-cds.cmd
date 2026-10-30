@echo off
REM CDS EOD after download_ready (17:00 + 30m = 17:30 IST). Clock assumed IST.
setlocal
cd /d "%~dp0.."
if not exist "data\ohlcv\_eod" mkdir "data\ohlcv\_eod"
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set STAMP=%%I
uv run ohlcv eod --markets cds --no-wait >> "data\ohlcv\_eod\cds-%STAMP%.log" 2>&1
exit /b %ERRORLEVEL%
