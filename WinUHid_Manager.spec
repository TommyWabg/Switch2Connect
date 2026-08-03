# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['src\\winuhid_manager.py'],
    pathex=['src'],
    binaries=[],
    datas=[('drivers\\install_driver.ps1', 'drivers'), ('drivers\\uninstall_driver.ps1', 'drivers'), ('drivers\\WinUHidDriver.inf', 'drivers'), ('drivers\\WinUHidDriver.dll', 'drivers'), ('drivers\\winuhiddriver.cat', 'drivers'), ('drivers\\WinUHidDriver.cer', 'drivers')],
    hiddenimports=[],
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
    name='WinUHid_Manager',
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
