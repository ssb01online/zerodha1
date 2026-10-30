@echo off
REM MCX EOD after download_ready (23:30 + 30m = 00:00 next day). Clock assumed IST.
REM Before 06:00 IST, ohlcv eod resolves trade_date to yesterday.
setlocal
cd /d "%~dp0.."
if not exist "data\ohlcv\_eod" mkdir "data\ohlcv\_eod"
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set STAMP=%%I
uv run ohlcv eod --markets mcx --no-wait >> "data\ohlcv\_eod\mcx-%STAMP%.log" 2>&1
exit /b %ERRORLEVEL%
