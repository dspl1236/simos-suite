#!/usr/bin/env python3
"""
build_exe.py — Build script for simos_suite.exe

Run from the repo root on a Windows machine with all deps installed:
    python build_exe.py
    python build_exe.py --clean
    python build_exe.py --onedir       # one-dir build instead of one-file
    python build_exe.py --console      # keep console window (for debugging)
    python build_exe.py --no-upx       # skip UPX compression
    python build_exe.py --sign         # code-sign the EXE (requires signtool)

What it does:
    1. Verifies prerequisites (Python version, required packages)
    2. Runs headless smoke test — aborts if any test fails
    3. Generates version_info.txt for Windows VERSIONINFO resource
    4. Runs pyinstaller simos_suite.spec
    5. Verifies the EXE launches and exits cleanly (--version check)
    6. Prints EXE path and size

Prerequisites (Windows):
    pip install pyinstaller udsoncan bleak pyserial numpy pycryptodome
    pip install git+https://github.com/bri3d/sa2_seed_key.git
    # Optional: install UPX and add to PATH for smaller EXE
    # https://upx.github.io/
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import struct
import subprocess
import sys
import textwrap
import time


ROOT    = pathlib.Path(__file__).parent.resolve()
DIST    = ROOT / "dist"
BUILD   = ROOT / "build"
SPEC    = ROOT / "simos_suite.spec"
ENTRY   = ROOT / "simos_suite.py"
VER_TXT = ROOT / "version_info.txt"

VERSION = "0.1.0"
COMPANY = "dspl1236 / simos-suite contributors"
PRODUCT = "Simos Tuning Suite"
DESC    = "Open-source ECU tuning and diagnostics for VAG vehicles"
URL     = "https://github.com/dspl1236/simos-suite"


def _banner(msg: str) -> None:
    print(f"\n{'─'*60}")
    print(f"  {msg}")
    print(f"{'─'*60}")


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, **kw)
    if result.returncode != 0:
        print(f"  ERROR: command exited {result.returncode}")
        sys.exit(result.returncode)
    return result


def check_prerequisites() -> None:
    _banner("Checking prerequisites")

    # Python version
    if sys.version_info < (3, 10):
        print(f"  ERROR: Python 3.10+ required (got {sys.version})")
        sys.exit(1)
    print(f"  OK  Python {sys.version.split()[0]}")

    # PyInstaller
    try:
        import PyInstaller
        print(f"  OK  PyInstaller {PyInstaller.__version__}")
    except ImportError:
        print("  ERROR: PyInstaller not found. Run: pip install pyinstaller")
        sys.exit(1)

    # Required packages
    required = {
        "udsoncan":       "pip install udsoncan",
        "bleak":          "pip install bleak",
        "serial":         "pip install pyserial",
        "numpy":          "pip install numpy",
        "Crypto":         "pip install pycryptodome",
    }
    for pkg, install in required.items():
        try:
            __import__(pkg)
            print(f"  OK  {pkg}")
        except ImportError:
            print(f"  WARN {pkg} not found ({install})")
            print("       EXE will be built but may fail to run without it.")

    # sa2_seed_key
    try:
        from sa2_seed_key.sa2_seed_key import Sa2SeedKey  # noqa: F401
        print("  OK  sa2_seed_key")
    except ImportError:
        print("  WARN sa2_seed_key not found")
        print("       pip install git+https://github.com/bri3d/sa2_seed_key.git")

    # UPX (optional)
    if shutil.which("upx"):
        result = subprocess.run(["upx", "--version"],
                                capture_output=True, text=True)
        ver = result.stdout.split("\n")[0].strip()
        print(f"  OK  UPX ({ver})")
    else:
        print("  INFO UPX not in PATH — EXE will not be compressed")
        print("       Download from https://upx.github.io/ for smaller output")


def run_smoke_test() -> None:
    _banner("Running headless smoke test")
    _run([sys.executable, "-m", "tests.sim_runner", "--headless"],
         cwd=ROOT)
    print("  PASS")


def generate_version_info() -> None:
    """Generate version_info.txt for Windows VERSIONINFO resource."""
    _banner("Generating version_info.txt")

    major, minor, patch = (int(x) for x in VERSION.split(".")[:3])

    content = textwrap.dedent(f"""\
    VSVersionInfo(
      ffi=FixedFileInfo(
        filevers=({major}, {minor}, {patch}, 0),
        prodvers=({major}, {minor}, {patch}, 0),
        mask=0x3f,
        flags=0x0,
        OS=0x40004,
        fileType=0x1,
        subtype=0x0,
        date=(0, 0)
      ),
      kids=[
        StringFileInfo([
          StringTable(
            u'040904B0',
            [
              StringStruct(u'CompanyName',      u'{COMPANY}'),
              StringStruct(u'FileDescription',  u'{DESC}'),
              StringStruct(u'FileVersion',      u'{VERSION}'),
              StringStruct(u'InternalName',     u'simos_suite'),
              StringStruct(u'LegalCopyright',   u'GPL v3 — {URL}'),
              StringStruct(u'OriginalFilename', u'simos_suite.exe'),
              StringStruct(u'ProductName',      u'{PRODUCT}'),
              StringStruct(u'ProductVersion',   u'{VERSION}'),
            ]
          )
        ]),
        VarFileInfo([VarStruct(u'Translation', [0x0409, 1200])])
      ]
    )
    """)
    VER_TXT.write_text(content)
    print(f"  Written {VER_TXT}")


def build(onedir: bool = False, console: bool = False,
          no_upx: bool = False) -> pathlib.Path:
    _banner("Building EXE")

    # Patch spec if needed
    spec_content = SPEC.read_text()

    if onedir:
        # comment out one-file section, uncomment COLLECT
        spec_content = spec_content.replace(
            "# coll = COLLECT(",
            "coll = COLLECT(")
        spec_content = spec_content.replace(
            "# )",
            ")")
        SPEC.write_text(spec_content)

    if console:
        spec_content = SPEC.read_text()
        spec_content = spec_content.replace("console=False", "console=True")
        SPEC.write_text(spec_content)

    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm"]
    if no_upx:
        cmd.append("--noupx")
    cmd.append(str(SPEC))

    _run(cmd, cwd=ROOT)

    if onedir:
        exe = DIST / "simos_suite" / "simos_suite.exe"
    else:
        exe = DIST / "simos_suite.exe"

    if not exe.exists():
        # Try without .exe (Linux/macOS)
        exe = DIST / "simos_suite"

    if not exe.exists():
        print(f"  ERROR: EXE not found at expected path {exe}")
        sys.exit(1)

    size_mb = exe.stat().st_size / 1024 / 1024
    print(f"\n  Built: {exe}")
    print(f"  Size:  {size_mb:.1f} MB")
    return exe


def verify_exe(exe: pathlib.Path) -> None:
    _banner("Verifying EXE")

    # --version should print and exit 0
    _run([str(exe), "--version"])
    print("  PASS  --version exit 0")

    # --headless should run all backend tests and exit 0
    _run([str(exe), "--headless"])
    print("  PASS  --headless exit 0")


def sign_exe(exe: pathlib.Path) -> None:
    _banner("Code-signing EXE")
    signtool = shutil.which("signtool")
    if not signtool:
        print("  WARN signtool not found — skipping")
        return
    # Requires a certificate — adjust as needed
    _run([signtool, "sign", "/a", "/fd", "SHA256",
          "/d", PRODUCT, "/du", URL, str(exe)])
    print("  OK  signed")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build simos_suite.exe")
    ap.add_argument("--clean",    action="store_true", help="Remove dist/ build/ first")
    ap.add_argument("--onedir",   action="store_true", help="One-dir instead of one-file")
    ap.add_argument("--console",  action="store_true", help="Keep console window")
    ap.add_argument("--no-upx",   action="store_true", help="Skip UPX compression")
    ap.add_argument("--no-test",  action="store_true", help="Skip smoke test")
    ap.add_argument("--sign",     action="store_true", help="Code-sign the EXE")
    args = ap.parse_args()

    os.chdir(ROOT)

    if args.clean:
        _banner("Cleaning build artifacts")
        for d in (DIST, BUILD):
            if d.exists():
                shutil.rmtree(d)
                print(f"  Removed {d}")

    check_prerequisites()

    if not args.no_test:
        run_smoke_test()

    generate_version_info()
    exe = build(onedir=args.onedir, console=args.console, no_upx=args.no_upx)
    verify_exe(exe)

    if args.sign:
        sign_exe(exe)

    _banner("Build complete")
    print(f"  EXE: {exe}")
    print(f"  Size: {exe.stat().st_size / 1024 / 1024:.1f} MB")
    print()
    print("  To distribute, ship simos_suite.exe — no Python install required.")
    print("  Users need Bluetooth LE hardware for BLE mode.")
    print("  J2534 DLL (Tactrix/Mongoose) needed for J2534 mode.")
    print()
    print("  Run: simos_suite.exe --help")


if __name__ == "__main__":
    main()
