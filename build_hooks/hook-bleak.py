"""
build_hooks/hook-bleak.py — PyInstaller hook for bleak BLE library

Ensures all bleak backends and their dependencies are collected,
especially the Windows WinRT backend used for BLE scanning.
"""
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = collect_all('bleak')

# WinRT backend for Windows BLE
hiddenimports += collect_submodules('bleak.backends.winrt')
hiddenimports += collect_submodules('bleak.backends.dotnet')
hiddenimports += [
    'bleak.backends.winrt.client',
    'bleak.backends.winrt.scanner',
    'bleak.backends.winrt.service',
    'bleak.backends.winrt.characteristic',
    'bleak.backends.winrt.descriptor',
    'winrt.windows.devices.bluetooth',
    'winrt.windows.devices.bluetooth.advertisement',
    'winrt.windows.devices.bluetooth.genericattributeprofile',
    'winrt.windows.foundation',
    'winrt.windows.foundation.collections',
    'winrt.windows.storage.streams',
]
