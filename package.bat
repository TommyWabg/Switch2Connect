@echo off
setlocal

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
echo Building Switch2Connect_%APP_VERSION%.exe

REM Remove the spec files this script generated on earlier runs. PyInstaller writes one
REM named after --name, so bumping the version would otherwise leave a new orphan behind
REM every release. Only this script's own specs are touched: the "_log" ones belong to
REM package_with_log.bat, and gui.spec is left alone entirely.
if exist "Switch2Connect.spec" del /q "Switch2Connect.spec"
for %%f in ("Switch2Connect_v*.spec") do (
    echo %%~nf| findstr /i /e "_log" >nul || del /q "%%f"
)

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

powershell -NoProfile -ExecutionPolicy Bypass -Command "$path = 'package_temp\config.yaml'; $content = [System.IO.File]::ReadAllText($path); $settings = @('driver_installed', 'hidhide_install_prompt_suppressed'); foreach ($setting in $settings) { $pattern = '(?m)^' + [regex]::Escape($setting) + ':\s*(true|false)\s*$'; if ($content -notmatch $pattern) { throw ($setting + ' setting not found.') }; $content = [regex]::Replace($content, $pattern, ($setting + ': false')) }; [System.IO.File]::WriteAllText($path, $content, [System.Text.UTF8Encoding]::new($false))"
if errorlevel 1 (
    echo Failed to reset package-only settings.
    rmdir /S /Q "%PACKAGE_CONFIG_DIR%" >nul 2>nul
    pause
    exit /b 1
)

REM Adopt the reset config as the repository copy, so the file in the project root always
REM matches what ships inside the exe. The reset copy was made from this same file and
REM only forces driver_installed and hidhide_install_prompt_suppressed to false, so this
REM changes nothing else. Done before the build, so the two stay in step even if the
REM build then fails.
copy /Y "%PACKAGE_CONFIG_FILE%" "%CONFIG_FILE%" >nul
if errorlevel 1 (
    echo Failed to update %CONFIG_FILE% with the reset settings.
    rmdir /S /Q "%PACKAGE_CONFIG_DIR%" >nul 2>nul
    pause
    exit /b 1
)

python -m PyInstaller --noconsole --onefile --clean --paths src --add-binary "drivers/WinUHid.dll;drivers" --add-binary "drivers/WinUHidDevs.dll;drivers" --add-data "resources;resources" --add-data "%PACKAGE_CONFIG_FILE%;resources" --add-data "drivers/install_driver.ps1;drivers" --add-data "drivers/install.bat;drivers" --add-data "drivers/uninstall_driver.ps1;drivers" --add-data "drivers/uninstall.bat;drivers" --add-data "drivers/uninstall_vigembus.ps1;drivers" --add-data "drivers/uninstall_vigembus.bat;drivers" --add-data "drivers/USBip-0.9.7.7-x64.exe;drivers" --add-data "drivers/install_usbip.ps1;drivers" --add-data "drivers/uninstall_usbip.ps1;drivers" --add-data "drivers/WinUHidDriver.inf;drivers" --add-data "drivers/WinUHidDriver.dll;drivers" --add-data "drivers/winuhiddriver.cat;drivers" --add-data "drivers/WinUHidDriver.cer;drivers" --add-data "drivers/esp32s3;drivers/esp32s3" --add-data "drivers/hidhide;drivers/hidhide" --add-data "firmware_bin;firmware_bin" --add-binary "drivers/dualsense_haptic_native.dll;drivers" --add-data "src;src" --collect-all vgamepad --collect-all imufusion --collect-all bleak --collect-all winrt --collect-all bluetooth --collect-all hid --collect-all libusb_package --collect-all comtypes --hidden-import imufusion --hidden-import hid --hidden-import usb.core --hidden-import usb.util --hidden-import libusb_package --hidden-import driver_install_helper --hidden-import usb_hid_controller --hidden-import hidhide --hidden-import usbip_server --hidden-import usbip_dualsense_server --hidden-import dualsense_descriptors --hidden-import dualsense_structs --hidden-import dualsense_haptic --hidden-import audio_endpoint_guard --hidden-import comtypes --hidden-import comtypes.client --hidden-import comtypes.automation --name "Switch2Connect_%APP_VERSION%" --icon="resources/images/icon.ico" src/gui.py
set "BUILD_EXIT=%ERRORLEVEL%"

rmdir /S /Q "%PACKAGE_CONFIG_DIR%" >nul 2>nul
pause
exit /b %BUILD_EXIT%
