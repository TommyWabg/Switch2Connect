# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('resources', 'resources'), ('config.yaml', 'resources'), ('drivers/install_driver.ps1', 'drivers'), ('drivers/install.bat', 'drivers'), ('drivers/uninstall_driver.ps1', 'drivers'), ('drivers/uninstall.bat', 'drivers'), ('drivers/uninstall_vigembus.ps1', 'drivers'), ('drivers/uninstall_vigembus.bat', 'drivers'), ('drivers/USBip-0.9.7.7-x64.exe', 'drivers'), ('drivers/install_usbip.ps1', 'drivers'), ('drivers/uninstall_usbip.ps1', 'drivers'), ('drivers/WinUHidDriver.inf', 'drivers'), ('drivers/WinUHidDriver.dll', 'drivers'), ('drivers/winuhiddriver.cat', 'drivers'), ('drivers/WinUHidDriver.cer', 'drivers'), ('drivers/esp32s3', 'drivers/esp32s3'), ('drivers/tools', 'drivers/tools'), ('firmware_bin', 'firmware_bin'), ('src', 'src')]
binaries = [('drivers/WinUHid.dll', 'drivers'), ('drivers/WinUHidDevs.dll', 'drivers')]
hiddenimports = ['imufusion', 'usbip_server', 'usbip_dualsense_server', 'dualsense_descriptors', 'dualsense_structs', 'dualsense_haptic']
tmp_ret = collect_all('vgamepad')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('imufusion')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('bleak')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('winrt')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('bluetooth')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['src\\gui.py'],
    pathex=['src'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Switch2Controllers',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['resources\\images\\icon.ico'],
)
