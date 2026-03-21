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
          # stdlib modules pkg_resources needs — must be explicit in PyInstaller
          'email',
          'email.mime',
          'email.mime.text',
          'email.mime.multipart',
          'email.mime.base',
          'email.generator',
          'email.parser',
          'email.policy',
          'pkg_resources',
          'pkg_resources._vendor',
          'pkg_resources.extern',
          # project modules
          'sa2_seed_key',
          'sa2_seed_key.sa2_script',
          'udsoncan',
          'bleak',
          'websocket',
          'serial',
          'serial.tools',
          'serial.tools.list_ports',
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
        'email',
        'html',
        'http',
        'urllib3',
        'multiprocessing',
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
