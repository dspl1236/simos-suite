# -*- mode: python ; coding: utf-8 -*-
# simos_suite.spec — PyInstaller build specification
#
# Build:
#   pyinstaller simos_suite.spec
#
# Output: dist/SimosSuite.exe  (single-file, Windows x64)
#
# Requirements before building:
#   pip install pyinstaller udsoncan bleak pyserial numpy pycryptodome
#   pip install git+https://github.com/bri3d/sa2_seed_key.git
#   pip install python-can
#
# Notes:
#   - bleak uses asyncio + WinRT on Windows — all collected automatically
#   - udsoncan is pure Python, no hidden imports needed
#   - sa2_seed_key bytecode interpreter is pure Python
#   - J2534 DLL loaded at runtime via ctypes — no static linking needed
#   - numpy: PyInstaller handles numpy hooks automatically since v5
#   - tkinter is bundled with the Python installer on Windows
#     (if missing: reinstall Python with "tcl/tk and IDLE" option checked)

import sys
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# ── Collect data files ─────────────────────────────────────────────────────────
# cp_routine_id.json must ship inside the EXE so the probe works offline
datas = [
    ('cp_tools/cp_routine_id.json', 'cp_tools'),
]

# bleak ships platform-specific backends — collect them all
try:
    datas += collect_data_files('bleak')
except Exception:
    pass

# ── Hidden imports ─────────────────────────────────────────────────────────────
# Modules loaded dynamically that PyInstaller's static analysis misses

hiddenimports = [
    # udsoncan internal services — loaded by name at runtime
    'udsoncan',
    'udsoncan.services',
    'udsoncan.services.DiagnosticSessionControl',
    'udsoncan.services.SecurityAccess',
    'udsoncan.services.ReadDataByIdentifier',
    'udsoncan.services.WriteDataByIdentifier',
    'udsoncan.services.RoutineControl',
    'udsoncan.services.RequestDownload',
    'udsoncan.services.TransferData',
    'udsoncan.services.RequestTransferExit',
    'udsoncan.services.ECUReset',
    'udsoncan.services.ReadMemoryByAddress',
    'udsoncan.client',
    'udsoncan.connections',
    'udsoncan.configs',
    'udsoncan.exceptions',

    # sa2_seed_key — loaded by name in security access
    'sa2_seed_key',
    'sa2_seed_key.sa2_seed_key',

    # bleak Windows backend
    'bleak',
    'bleak.backends',
    'bleak.backends.winrt',
    'bleak.backends.winrt.client',
    'bleak.backends.winrt.scanner',
    'bleak.backends.winrt.service',
    'bleak.backends.winrt.characteristic',
    'bleak.backends.winrt.descriptor',
    'bleak.backends.dotnet',     # fallback
    'bleak.backends.p4android',  # not used but avoids import error
    'bleak.exc',

    # python-can backends (only USB-SocketCAN used but collect all)
    'can',
    'can.interfaces',
    'can.interfaces.socketcan',
    'can.interfaces.usb2can',
    'can.interfaces.vector',
    'can.interfaces.kvaser',
    'can.interfaces.pcan',
    'can.interfaces.ixxat',
    'can.interfaces.nican',
    'can.interfaces.iscan',
    'can.interfaces.neovi',
    'can.interfaces.gs_usb',
    'can.interfaces.virtual',
    'can.interfaces.slcan',
    'can.interfaces.systec',
    'can.listener',
    'can.util',
    'can.message',
    'can.notifier',
    'can.typechecking',

    # pyserial
    'serial',
    'serial.serialwin32',
    'serial.serialutil',
    'serial.tools',
    'serial.tools.list_ports',
    'serial.tools.list_ports_windows',

    # numpy and its lazy-loaded submodules
    'numpy',
    'numpy.core',
    'numpy.core._multiarray_umath',
    'numpy.lib',
    'numpy.random',

    # pycryptodome (AES for Simos12/18)
    'Crypto',
    'Crypto.Cipher',
    'Crypto.Cipher.AES',
    'Crypto.Util',
    'Crypto.Util.Padding',

    # tkinter extras sometimes missed
    'tkinter',
    'tkinter.ttk',
    'tkinter.filedialog',
    'tkinter.messagebox',
    'tkinter.scrolledtext',
    'tkinter.font',
    '_tkinter',

    # asyncio event loop for bleak on Windows
    'asyncio',
    'asyncio.events',
    'asyncio.windows_events',
    'asyncio.proactor_events',

    # winrt (bleak Windows BLE backend, Python 3.12+)
    'winrt',
    'winrt.windows.devices.bluetooth',
    'winrt.windows.devices.bluetooth.advertisement',
    'winrt.windows.devices.bluetooth.genericattributeprofile',
    'winrt.windows.foundation',
    'winrt.windows.foundation.collections',
    'winrt.windows.storage.streams',

    # Our own modules — all subpackages
    'core',
    'core.ecu_defs',
    'core.trans_defs',
    'cp_tools',
    'cp_tools.j533_probe',
    'cp_tools.odx_parser',
    'cp_tools.mwb_extract',
    'flasher',
    'flasher.uds_flash',
    'lib.connections',
    'lib.connections.j2534',
    'lib.connections.j2534_connection',
    'lib.connections.usb_isotp_connection',
    'logger',
    'transport',
    'transport.ble_bridge',
    'transport.interfaces',
    'tuner',
    'tuner.cal_parser',
    'ui',
    'ui.main_window',
    'ui.trans_logger',
    'ui.trans_tab',
    'ui.interface_panel',
    'ui.hardware_tab',
]

# ── Analysis ───────────────────────────────────────────────────────────────────

a = Analysis(
    ['simos_suite_main.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=['build_hooks'],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude heavy unused packages
        'matplotlib',
        'scipy',
        'pandas',
        'PIL',
        'PIL.Image',
        'wx',
        'PyQt5',
        'PyQt6',
        'PySide2',
        'PySide6',
        'IPython',
        'jupyter',
        'notebook',
        'pytest',
        'setuptools',
        'distutils',
        'email',
        'html',
        'http',
        'urllib',
        'xml',
        'xmlrpc',
        'ftplib',
        'imaplib',
        'poplib',
        'smtplib',
        'telnetlib',
        'nntplib',
        'unittest',
        'doctest',
        'pdb',
        'profile',
        'cProfile',
        'pstats',
        'timeit',
        'trace',
        'turtle',
        'tkinter.test',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ── Single-file EXE ───────────────────────────────────────────────────────────

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='SimosSuite',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,           # compress with UPX if available (smaller EXE)
    upx_exclude=[
        # Don't compress these — causes issues with some AV/Windows
        'vcruntime140.dll',
        'python3*.dll',
        '_tkinter*.pyd',
    ],
    runtime_tmpdir=None,
    console=False,       # No console window — pure GUI app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,    # x64 by default on x64 Python
    codesign_identity=None,
    entitlements_file=None,
    # Windows-specific metadata
    version='version_info.txt',
    icon='build_assets/simos_suite.ico',
    uac_admin=False,     # Don't require admin — user-land tool
)
