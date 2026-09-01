@echo off
setlocal
cd /d "%~dp0"

set "ENTRY_PAGE=%CD%\docs\index.html"

if not exist "%ENTRY_PAGE%" (
    echo CoinPoker PLO Population Analyzer could not start.
    echo Missing file: "%ENTRY_PAGE%"
    echo Please extract the complete project folder and try again.
    pause
    exit /b 1
)

if /I "%~1"=="--check" exit /b 0

start "" "%ENTRY_PAGE%"
exit /b 0
