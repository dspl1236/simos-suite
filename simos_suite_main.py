"""
simos_suite_main.py — PyInstaller entry point

Single-file entry point for the Windows EXE.
Run: python simos_suite_main.py [--ecu S85] [--debug]
"""
import sys
import os

# When frozen by PyInstaller, add the _MEIPASS temp directory to sys.path
# so all our modules are importable
if getattr(sys, 'frozen', False):
    _base = sys._MEIPASS
    sys.path.insert(0, _base)

from ui.main_window import main

if __name__ == '__main__':
    main()
