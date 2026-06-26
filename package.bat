@echo off
setlocal

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

copy /Y "%CONFIG_FILE%" "%PACKAGE_CONFIG_FILE%" >nul
if errorlevel 1 (
    echo Failed to create package config.
    rmdir /S /Q "%PACKAGE_CONFIG_DIR%" >nul 2>nul
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$path = 'package_temp\config.yaml'; $content = [System.IO.File]::ReadAllText($path); if ($content -notmatch '(?m)^driver_installed:\s*(true|false)\s*$') { throw 'driver_installed setting not found.' }; $content = [regex]::Replace($content, '(?m)^driver_installed:\s*(true|false)\s*$', 'driver_installed: false'); [System.IO.File]::WriteAllText($path, $content, [System.Text.UTF8Encoding]::new($false))"
if errorlevel 1 (
    echo Failed to set driver_installed to false.
    rmdir /S /Q "%PACKAGE_CONFIG_DIR%" >nul 2>nul
    pause
    exit /b 1
)

python -m PyInstaller --noconsole --onefile --clean --paths src --add-binary "drivers/WinUHid.dll;drivers" --add-binary "drivers/WinUHidDevs.dll;drivers" --add-data "resources;resources" --add-data "%PACKAGE_CONFIG_FILE%;resources" --add-data "drivers/install_driver.ps1;drivers" --add-data "drivers/install.bat;drivers" --add-data "drivers/uninstall_driver.ps1;drivers" --add-data "drivers/uninstall.bat;drivers" --add-data "drivers/uninstall_vigembus.ps1;drivers" --add-data "drivers/uninstall_vigembus.bat;drivers" --add-data "drivers/USBip-0.9.7.7-x64.exe;drivers" --add-data "drivers/install_usbip.ps1;drivers" --add-data "drivers/uninstall_usbip.ps1;drivers" --add-data "drivers/WinUHidDriver.inf;drivers" --add-data "drivers/WinUHidDriver.dll;drivers" --add-data "drivers/winuhiddriver.cat;drivers" --add-data "drivers/WinUHidDriver.cer;drivers" --add-data "drivers/esp32s3;drivers/esp32s3" --add-data "firmware_bin;firmware_bin" --add-data "src;src" --collect-all vgamepad --collect-all imufusion --collect-all bleak --collect-all winrt --collect-all bluetooth --hidden-import imufusion --hidden-import usbip_server --hidden-import usbip_dualsense_server --hidden-import dualsense_descriptors --hidden-import dualsense_structs --hidden-import dualsense_haptic --name "Switch2Controllers" --icon="resources/images/icon.ico" src/gui.py
set "BUILD_EXIT=%ERRORLEVEL%"

rmdir /S /Q "%PACKAGE_CONFIG_DIR%" >nul 2>nul
pause
exit /b %BUILD_EXIT%
