"""
build_hooks/hook-sa2_seed_key.py — PyInstaller hook for sa2_seed_key
"""
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = collect_all('sa2_seed_key')
