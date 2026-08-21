@echo off
setlocal
set "PYTHON_COMMAND=python"

REM ============================================================================
REM  Diagnostic build: identical to package.bat except the exe keeps a console.
REM
REM  The shipping build is packaged with --noconsole, which leaves sys.stderr as
REM  None. The logging StreamHandler in src/controller.py writes to stderr, so a
REM  --noconsole exe produces NO log at all -- there is nothing to ask a reporter
REM  to send. This build uses --console so the full log is visible and can be
REM  redirected to a file. Its runtime hook also enables the opt-in System
REM  Bluetooth Joy-Con diagnostic timeline before the application starts.
REM
REM  Output: dist\Switch2Connect_<version>_log.exe (does not overwrite the normal build).
REM ============================================================================

REM Read the version straight out of src\gui.py so the exe name can never drift from
REM what the app reports in its UI. Parsed rather than imported: importing gui.py would
REM run the whole module.
set "APP_VERSION="
for /f "tokens=2 delims==" %%v in ('findstr /b /c:"APP_VERSION" src\gui.py') do set RAW_VERSION=%%v
set RAW_VERSION=%RAW_VERSION: =%
set RAW_VERSION=%RAW_VERSION:"=%
set "APP_VERSION=%RAW_VERSION%"

if "%APP_VERSION%"=="" (
    echo Could not read APP_VERSION from src\gui.py.
    pause
    exit /b 1
)
echo Building Switch2Connect_%APP_VERSION%_log.exe

REM Remove the spec files this script generated on earlier runs. PyInstaller writes one
REM named after --name, so bumping the version would otherwise leave a new orphan behind
REM every release. Only the "_log" specs are touched -- package.bat cleans up its own.
if exist "Switch2Connect_log.spec" del /q "Switch2Connect_log.spec"
for %%f in ("Switch2Connect_v*_log.spec") do del /q "%%f"

set "CONFIG_FILE=config.yaml"
set "PACKAGE_CONFIG_DIR=package_temp"
set "PACKAGE_CONFIG_FILE=%PACKAGE_CONFIG_DIR%\config.yaml"

if not exist "%CONFIG_FILE%" (
    echo Missing %CONFIG_FILE%.
    pause
    exit /b 1
)

if exist "%PACKAGE_CONFIG_DIR%" rmdir /S /Q "%PACKAGE_CONFIG_DIR%"
mkdir "%PACKAGE_CONFIG_DIR%"
if errorlevel 1 (
    echo Failed to create %PACKAGE_CONFIG_DIR%.
    pause
    exit /b 1
)

REM This marker is bundled only by the diagnostic package.  Runtime code uses
REM it to expose Mag Tester without relying on the executable filename.
> "%PACKAGE_CONFIG_DIR%\mag_tester_enabled.marker" echo Switch2Connect Mag Tester diagnostic build

if not exist "drivers\dualsense_haptic_native.dll" (
    echo Missing drivers\dualsense_haptic_native.dll.
    echo Build it first: powershell -ExecutionPolicy Bypass -File native\build_dualsense_haptic_native.ps1
    pause
    exit /b 1
)

copy /Y "%CONFIG_FILE%" "%PACKAGE_CONFIG_FILE%" >nul
if errorlevel 1 (
    echo Failed to create package config.
    rmdir /S /Q "%PACKAGE_CONFIG_DIR%" >nul 2>nul
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "tools\prepare_package_config.ps1" -Path "%PACKAGE_CONFIG_FILE%"
if errorlevel 1 (
    echo Failed to reset package-only settings.
    rmdir /S /Q "%PACKAGE_CONFIG_DIR%" >nul 2>nul
    pause
    exit /b 1
)

REM Adopt the reset config as the repository copy, so the file in the project root always
REM matches what ships inside the exe. The reset copy was made from this same file and
REM only forces package defaults (including Power Saving Off and both warnings enabled), so this
REM changes nothing else. Done before the build, so the two stay in step even if the
REM build then fails.
copy /Y "%PACKAGE_CONFIG_FILE%" "%CONFIG_FILE%" >nul
if errorlevel 1 (
    echo Failed to update %CONFIG_FILE% with the reset settings.
    rmdir /S /Q "%PACKAGE_CONFIG_DIR%" >nul 2>nul
    pause
    exit /b 1
)

%PYTHON_COMMAND% -m PyInstaller --console --onefile --clean --runtime-hook "tools/enable_system_bt_diagnostics.py" --paths src --add-binary "drivers/WinUHid.dll;drivers" --add-binary "drivers/WinUHidDevs.dll;drivers" --add-data "resources;resources" --add-data "%PACKAGE_CONFIG_FILE%;resources" --add-data "%PACKAGE_CONFIG_DIR%\mag_tester_enabled.marker;." --add-data "drivers/install_driver.ps1;drivers" --add-data "drivers/install.bat;drivers" --add-data "drivers/uninstall_driver.ps1;drivers" --add-data "drivers/uninstall.bat;drivers" --add-data "drivers/uninstall_vigembus.ps1;drivers" --add-data "drivers/uninstall_vigembus.bat;drivers" --add-data "drivers/USBip-0.9.7.7-x64.exe;drivers" --add-data "drivers/install_usbip.ps1;drivers" --add-data "drivers/uninstall_usbip.ps1;drivers" --add-data "drivers/WinUHidDriver.inf;drivers" --add-data "drivers/WinUHidDriver.dll;drivers" --add-data "drivers/winuhiddriver.cat;drivers" --add-data "drivers/WinUHidDriver.cer;drivers" --add-data "drivers/esp32s3;drivers/esp32s3" --add-data "drivers/hidhide;drivers/hidhide" --add-data "firmware_bin;firmware_bin" --add-binary "drivers/dualsense_haptic_native.dll;drivers" --add-data "src;src" --collect-all vgamepad --collect-all imufusion --collect-all bleak --collect-all winrt --collect-all bluetooth --collect-all hid --collect-all libusb_package --collect-all comtypes --hidden-import imufusion --hidden-import hid --hidden-import usb.core --hidden-import usb.util --hidden-import libusb_package --hidden-import driver_install_helper --hidden-import usb_hid_controller --hidden-import hidhide --hidden-import usbip_server --hidden-import usbip_dualsense_server --hidden-import dualsense_descriptors --hidden-import dualsense_structs --hidden-import dualsense_haptic --hidden-import audio_endpoint_guard --hidden-import comtypes --hidden-import comtypes.client --hidden-import comtypes.automation --name "Switch2Connect_%APP_VERSION%_log" --icon="resources/images/icon.ico" src/gui.py
set "BUILD_EXIT=%ERRORLEVEL%"

rmdir /S /Q "%PACKAGE_CONFIG_DIR%" >nul 2>nul

if "%BUILD_EXIT%"=="0" (
    echo.
    echo ==========================================================================
    echo  Built: dist\Switch2Connect_%APP_VERSION%_log.exe
    echo  System Bluetooth Joy-Con diagnostics: enabled automatically
    echo.
    echo  To save the log to a file, run it from a terminal rather than
    echo  double-clicking it:
    echo.
    echo      dist\Switch2Connect_%APP_VERSION%_log.exe ^> wired-test.log 2^>^&1
    echo.
    echo  WARNING: do not select text in the console window while testing.
    echo  Windows QuickEdit freezes every thread that writes a log record, which
    echo  stalls controller I/O and will corrupt your test results. To remove the
    echo  risk entirely: right-click the console title bar, Properties, and
    echo  uncheck "QuickEdit Mode".
    echo ==========================================================================
    echo.
)

pause
exit /b %BUILD_EXIT%
