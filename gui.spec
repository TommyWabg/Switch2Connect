# -*- mode: python ; coding: utf-8 -*-
import os
import sys
import pkgutil
import PySide6
sys.path.insert(0, os.path.join(SPECPATH, "src"))
from PyInstaller.utils.hooks import collect_all, collect_submodules, collect_data_files

datas = [('resources', 'resources'), ('config.yaml', 'resources'), ('drivers/esp32s3', 'drivers/esp32s3'), ('drivers/tools', 'drivers/tools')]
binaries = []
if os.path.exists('drivers/dualsense_haptic_native.dll'):
    binaries.append(('drivers/dualsense_haptic_native.dll', 'drivers'))
hiddenimports = [
    'driver_install_helper',
    'usbip_server',
    'usbip_dualsense_server',
    'dualsense_descriptors',
    'dualsense_structs',
    'dualsense_haptic',
    'audio_endpoint_guard',
    'esp32s3_bridge',
    'comtypes',
    'comtypes.client',
    'comtypes.automation',
    'win32com',
    'win32com.client',
    'kofi_webview',
    'webview',
    'clr',
    'proxy_tools',
    'bottle',
]
tmp_ret = collect_all('vgamepad')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('imufusion')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('comtypes')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('webview')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


# Dynamic page factories and CLI-selected composition roots are invisible to analysis.
hiddenimports += collect_submodules('ui_modern') + collect_submodules('app_core')
hiddenimports += collect_submodules('qfluentwidgets') + collect_submodules('qframelesswindow')
datas += collect_data_files('qfluentwidgets') + collect_data_files('qframelesswindow')
# PySide6 hooks collect plugins for modules actually used; never collect_all('PySide6').
qt_excludes = ['PySide6.' + name for name in (
    'QtWebEngineCore', 'QtWebEngineWidgets', 'QtWebEngineQuick', 'QtWebChannel',
    'Qt3DCore', 'Qt3DRender', 'Qt3DInput', 'Qt3DLogic', 'Qt3DAnimation', 'Qt3DExtras',
    'QtMultimedia', 'QtMultimediaWidgets', 'QtQuick', 'QtQuickWidgets', 'QtQuickControls2',
    'QtQuick3D', 'QtQuickTest', 'QtQml', 'QtQmlModels', 'QtQmlWorkerScript',
    'QtCharts', 'QtDataVisualization', 'QtGraphs', 'QtGraphsWidgets', 'QtPdf', 'QtPdfWidgets',
    'QtSql', 'QtTest', 'QtBluetooth', 'QtPositioning', 'QtLocation', 'QtRemoteObjects',
    'QtSensors', 'QtSerialBus', 'QtSerialPort', 'QtScxml', 'QtSpatialAudio',
    'QtTextToSpeech', 'QtVirtualKeyboard', 'QtHttpServer', 'QtNetworkAuth',
    'QtDesigner', 'QtHelp', 'QtUiTools')]
# Also cover future modules in these unused families, without importing them.
unused_qt_prefixes = ('QtWebEngine', 'Qt3D', 'QtMultimedia', 'QtQuick', 'QtQml', 'QtPdf')
qt_excludes += ['PySide6.' + module.name for module in pkgutil.iter_modules(PySide6.__path__)
                if module.name.startswith(unused_qt_prefixes)]
# Ko-fi uses WebView2 on Windows; its optional Qt adapter would pull WebEngine back in.
qt_excludes += ['webview.platforms.qt', 'PyQt5', 'PyQt6', 'PySide2']

a = Analysis(
    ['src/gui.py'],
    pathex=['.', 'src'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=qt_excludes,
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
    name='gui',
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
    icon='resources/images/icon.ico',
)
