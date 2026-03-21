# -*- mode: python ; coding: utf-8 -*-
#
# simos_suite.spec — PyInstaller build specification
#
# Build:
#   pyinstaller simos_suite.spec
#
# Output:
#   dist/simos_suite.exe            (Windows one-file EXE)
#   dist/simos_suite/simos_suite    (Linux/macOS one-dir build)
#
# Notes:
#   - Targets Python 3.10+ on 64-bit Windows
#   - J2534 PassThru DLL is loaded at runtime — not bundled
#   - BLE via bleak requires Windows BT stack (Bluetooth LE hardware)
#   - udsoncan, bleak, pyserial, pycryptodome must be pip-installed first
#
# Windows build machine prerequisites:
#   pip install pyinstaller udsoncan bleak pyserial numpy pycryptodome
#   pip install git+https://github.com/bri3d/sa2_seed_key.git
#   pyinstaller simos_suite.spec

import sys
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

# Collect all email submodules — pkg_resources runtime hook needs them
_email_imports = collect_submodules('email')

block_cipher = None

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT = Path(SPECPATH)

# ── Analysis ──────────────────────────────────────────────────────────────────
a = Analysis(
    ['simos_suite.py'],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        # Ship the CP research data file so the routine ID is available offline
        ('cp_tools/cp_routine_id.json', 'cp_tools'),
        # Ship the docs for the about dialog / help
        ('docs/tuning_guide_s85.md',   'docs'),
        ('docs/odx_findings.md',       'docs'),
        ('README.md',                  '.'),
        ('LICENSE',                    '.'),
    ],
    hiddenimports=[
          # stdlib — pkg_resources runtime hook needs all email submodules
          *_email_imports,
          'pkg_resources',
          'pkg_resources._vendor',
          'pkg_resources.extern',
          # udsoncan internals PyInstaller can miss
          'udsoncan',
          'udsoncan.client',
          'udsoncan.services',
          'udsoncan.configs',
          'udsoncan.exceptions',
          'udsoncan.connections',
          'udsoncan.Request',
          'udsoncan.Response',
          # bleak BLE backend — Windows needs the winRT backend
          'bleak',
          'bleak.backends.winrt.client',
          'bleak.backends.winrt.scanner',
          'bleak.backends.winrt.utils',
          # pyserial
          'serial',
          'serial.tools',
          'serial.tools.list_ports',
          'websocket',
          'websocket._core',
          'websocket._http',
          'websocket._handshake',
          'websocket._socket',
          'websocket._ssl_compat',
          'websocket._utils',
          # numpy (cal_parser)
          'numpy',
          'numpy.core._methods',
          'numpy.lib.format',
          # pycryptodome (Simos12/18 AES)
          'Crypto',
          'Crypto.Cipher',
          'Crypto.Cipher.AES',
          # sa2_seed_key (SA2 bytecode interpreter)
          'sa2_seed_key',
          'sa2_seed_key.sa2_seed_key',
          # python-can (SocketCAN — used on Linux, imported lazily on Windows)
          'can',
          'can.interfaces',
          'can.interfaces.socketcan',
          # our own packages
          'core',
          'core.ecu_defs',
          'core.trans_defs',
          'cp_tools',
          'cp_tools.j533_probe',
          'cp_tools.mwb_extract',
          'cp_tools.odx_parser',
          'flasher',
          'flasher.uds_flash',
          'lib',
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
          'ui.hardware_tab',
          'ui.interface_panel',
          'ui.trans_logger',
          'ui.trans_tab',
          'tests',
          'tests.mock_connection',
          'tests.sim_runner',
      ],
    excludes=[
        # exclude test/dev-only packages
        'pytest',
        'setuptools',
        'distutils',
        'pip',
        'IPython',
        'jupyter',
        'matplotlib',
        # exclude macOS-specific bleak backend
        'bleak.backends.corebluetooth',
        # exclude Linux bleak backend (Windows build)
        'bleak.backends.bluezdbus',
        # large unused modules
        'tkinter.test',
        'xmlrpc',
        'multiprocessing',
        # NOTE: do NOT exclude email, html, http, urllib3 —
        # pkg_resources runtime hook (pyi_rth_pkgres.py) needs them
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ── One-file EXE (Windows) ────────────────────────────────────────────────────
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
    upx=True,          # compress — cuts ~30% on most binaries
    upx_exclude=[
        # don't compress these — UPX breaks some DLLs
        'vcruntime140.dll',
        '_ssl.dll',
        'libssl*.dll',
        'libcrypto*.dll',
    ],
    runtime_tmpdir=None,
    console=False,      # no console window — log goes to %TEMP%\simos_suite.log
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Windows-specific:
    version='version_info.txt',   # see build_exe.py for auto-generation
    # icon='assets/simos_suite.ico',  # uncomment when icon is added
)

# ── One-dir build (Linux / macOS) ────────────────────────────────────────────
# Uncomment for Linux/macOS one-dir instead of one-file:
#
# coll = COLLECT(
#     exe,
#     a.binaries,
#     a.zipfiles,
#     a.datas,
#     strip=False,
#     upx=True,
#     upx_exclude=[],
#     name='SimosSuite',
# )
