@echo off
:: Check for administrator privileges
net session >nul 2>&1
if %errorLevel% == 0 (
    echo Running with Administrator privileges...
    cd /d "%~dp0"
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall_driver.ps1"
    exit /b
) else (
    echo Requesting Administrator privileges...
    powershell -Command "Start-Process -FilePath '%~0' -Verb RunAs -Wait"
    exit /b
)
