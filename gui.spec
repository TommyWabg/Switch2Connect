# -*- mode: python ; coding: utf-8 -*-
import os
import hashlib
import re
from pathlib import Path
from PyInstaller.archive.readers import CArchiveReader
import sys
import pkgutil
import PySide6
import yaml
sys.path.insert(0, os.path.join(SPECPATH, "src"))
from PyInstaller.utils.hooks import collect_all, collect_submodules, collect_data_files

root = Path(SPECPATH)
release = Path(os.environ.get("SWITCH2CONNECT_UPSTREAM_EXE") or
               root / "build/upstream-v3.4/Switch2Connect_v3.4.exe")
with release.open("rb") as stream:
    digest = hashlib.file_digest(stream, "sha256").hexdigest()
if digest != "5f2b61f4ba8f88c3f201710fd718676731308dad843e5a1fea7c5831eb8d2829":
    raise ValueError("The upstream executable must be the official Switch2Connect_v3.4.exe")

# Upstream publishes the current firmware manifest, driver installer and reset
# config in its release. Its source tree contains an older firmware manifest.
archive = CArchiveReader(str(release))
payload = Path(workpath) / "release-assets"
datas = [(str(p), str(p.parent.relative_to(root))) for p in (root / "src").rglob("*.py")]
binaries = []
resources = {p.relative_to(root): p for p in (root / "resources").rglob("*") if p.is_file()}
for name in archive.toc:
    rel = Path(name)
    if rel.parts[0] not in {"resources", "drivers", "firmware_bin"}:
        continue
    if rel.parts[0] == "resources" and rel in resources:
        continue
    local = root / rel
    if not local.is_file() or "firmware" in rel.parts or rel.parts[0] == "firmware_bin":
        local = payload / rel
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(archive.extract(name))
    if rel == Path("resources/config.yaml"):
        # Extend the official clean seed without reading a user's working config.
        seed = yaml.safe_load(local.read_bytes())
        preset = yaml.safe_load((root / "resources/profiles/Pro2-GL-Desktop.yaml").read_bytes())
        seed["profiles"].update(preset["profiles"])
        local = payload / rel
        local.write_text(yaml.safe_dump(seed, allow_unicode=True, sort_keys=False), encoding="utf-8")
    resources[rel] = local

native = {"WinUHid.dll", "WinUHidDevs.dll", "dualsense_haptic_native.dll"}
for rel, path in resources.items():
    collection = binaries if rel.name in native else datas
    collection.append((str(path), str(rel.parent)))

hiddenimports = [
    "driver_install_helper", "usb_hid_controller", "usbip_server", "usbip_dualsense_server",
    "dualsense_descriptors", "dualsense_structs", "dualsense_haptic", "audio_endpoint_guard",
    "usb.core", "usb.util", "win32com.client", "comtypes.client", "comtypes.automation",
    "kofi_webview", "clr", "proxy_tools", "bottle",
]
for package in ("vgamepad", "imufusion", "bleak", "winrt", "bluetooth", "hid", "libusb_package", "comtypes", "webview"):
    extra_data, extra_binaries, extra_imports = collect_all(package)
    datas.extend(extra_data)
    binaries.extend(extra_binaries)
    hiddenimports.extend(extra_imports)

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
# Qt for Windows imports the system ICU API. An unrelated ICU build on PATH
# can have the same filename and versioned exports that Qt cannot load.
a.binaries = [entry for entry in a.binaries if Path(entry[0]).name.lower() not in {"icuuc.dll", "icuin.dll"}]
pyz = PYZ(a.pure)
version = re.search(r'^APP_VERSION = "([^"]+)"', (root / "src/gui.py").read_text(encoding="utf8"), re.M)[1]

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=f"Switch2Connect_{version}",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
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
