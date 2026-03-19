"""
build_hooks/hook-udsoncan.py — PyInstaller hook for udsoncan
"""
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = collect_all('udsoncan')
hiddenimports += collect_submodules('udsoncan.services')
hiddenimports += collect_submodules('udsoncan.connections')
