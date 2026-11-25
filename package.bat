@echo off
setlocal
cd /d "%~dp0"
if defined SWITCH2CONNECT_UPSTREAM_EXE goto build
set "SWITCH2CONNECT_UPSTREAM_EXE=%CD%\build\upstream-v3.4\Switch2Connect_v3.4.exe"
if exist "%SWITCH2CONNECT_UPSTREAM_EXE%" goto build
if not exist "build\upstream-v3.4" mkdir "build\upstream-v3.4"
curl.exe --fail --location --retry 2 --output "build\upstream-v3.4\Switch2Connect_v3.4.exe.download" "https://github.com/TommyWabg/Switch2Connect/releases/download/v3.4/Switch2Connect_v3.4.exe"
if errorlevel 1 exit /b 1
move /y "build\upstream-v3.4\Switch2Connect_v3.4.exe.download" "build\upstream-v3.4\Switch2Connect_v3.4.exe" >nul
if errorlevel 1 exit /b 1
:build
python -m PyInstaller --noconfirm gui.spec
exit /b %errorlevel%
