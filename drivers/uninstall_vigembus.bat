@echo off
:: Check for administrator privileges
net session >nul 2>&1
if %errorLevel% == 0 (
    echo Running with Administrator privileges...
    cd /d "%~dp0"
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall_vigembus.ps1"
    pause
    exit /b
) else (
    echo Requesting Administrator privileges...
    powershell -Command "Start-Process -FilePath '%~0' -Verb RunAs"
    exit /b
)
