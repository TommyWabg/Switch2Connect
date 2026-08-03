@echo off
setlocal

REM Build the standalone WinUHid Manager distributed with the GitHub Full Version.
REM This package intentionally contains the WinUHid driver payload; it is never
REM included in the Microsoft Store MSIX build.

set "APP_NAME=WinUHid_Manager"
set "SPEC_FILE=%APP_NAME%.spec"
set "DIST_DIR=dist"
set "WORK_DIR=build\%APP_NAME%"
set "OUTPUT_EXE=%DIST_DIR%\%APP_NAME%.exe"

for %%F in (
    "drivers\install_driver.ps1"
    "drivers\uninstall_driver.ps1"
    "drivers\WinUHidDriver.inf"
    "drivers\WinUHidDriver.dll"
    "drivers\winuhiddriver.cat"
    "drivers\WinUHidDriver.cer"
    "resources\images\icon.ico"
) do (
    if not exist "%%~F" (
        echo Missing required file: %%~F
        pause
        exit /b 1
    )
)

if exist "%SPEC_FILE%" del /q "%SPEC_FILE%"
if exist "%OUTPUT_EXE%" del /q "%OUTPUT_EXE%"
if not exist "%DIST_DIR%" mkdir "%DIST_DIR%"
if not exist "build" mkdir "build"

echo Building %APP_NAME%.exe...
python -m PyInstaller --noconsole --onefile --clean --paths src ^
    --add-data "drivers\install_driver.ps1;drivers" ^
    --add-data "drivers\uninstall_driver.ps1;drivers" ^
    --add-data "drivers\WinUHidDriver.inf;drivers" ^
    --add-data "drivers\WinUHidDriver.dll;drivers" ^
    --add-data "drivers\winuhiddriver.cat;drivers" ^
    --add-data "drivers\WinUHidDriver.cer;drivers" ^
    --icon="resources\images\icon.ico" ^
    --name "%APP_NAME%" ^
    src\winuhid_manager.py
set "BUILD_EXIT=%ERRORLEVEL%"

if not "%BUILD_EXIT%"=="0" (
    echo WinUHid Manager build failed with exit code %BUILD_EXIT%.
    pause
    exit /b %BUILD_EXIT%
)

if not exist "%OUTPUT_EXE%" (
    echo Build completed but %OUTPUT_EXE% was not created.
    pause
    exit /b 1
)

echo.
echo Built: %OUTPUT_EXE%
certutil -hashfile "%OUTPUT_EXE%" SHA256
pause
exit /b 0
