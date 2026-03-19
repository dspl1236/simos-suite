"""
build_exe.py — Simos Suite EXE build helper

A thin wrapper around PyInstaller that:
  1. Verifies the headless smoke test passes before building
  2. Patches the .spec to remove missing optional files (icon, version_info)
  3. Calls PyInstaller
  4. Reports the output size

Usage:
    python build_exe.py                 # full build
    python build_exe.py --no-test       # skip smoke test (faster iteration)
    python build_exe.py --no-upx        # skip UPX compression (GitHub Actions)
    python build_exe.py --debug         # PyInstaller debug mode
"""
from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import subprocess
import sys


def run(cmd: list, **kw) -> int:
    print(f"\n$ {' '.join(str(c) for c in cmd)}")
    return subprocess.call(cmd, **kw)


def patch_spec(spec_path: pathlib.Path, no_icon: bool, no_version: bool) -> pathlib.Path:
    """Remove optional fields from .spec if the asset files are missing."""
    content = spec_path.read_text(encoding="utf-8")
    if no_icon:
        content = re.sub(r",\s*\n\s*icon\s*=\s*'[^']*'", "", content)
    if no_version:
        content = re.sub(r",\s*\n\s*version\s*=\s*'[^']*'", "", content)
    patched = spec_path.parent / "simos_suite_patched.spec"
    patched.write_text(content, encoding="utf-8")
    return patched


def main():
    ap = argparse.ArgumentParser(description="Simos Suite EXE builder")
    ap.add_argument("--no-test",  action="store_true", help="Skip pre-build smoke test")
    ap.add_argument("--no-upx",   action="store_true", help="Disable UPX compression")
    ap.add_argument("--debug",    action="store_true", help="PyInstaller --debug all")
    ap.add_argument("--onedir",   action="store_true", help="Build --onedir instead of --onefile")
    args = ap.parse_args()

    root = pathlib.Path(__file__).parent
    spec = root / "simos_suite.spec"
    dist = root / "dist"

    # ── Pre-build smoke test ──────────────────────────────────────────────────
    if not args.no_test:
        print("\n[1/3] Running headless smoke test...")
        rc = run([sys.executable, "-m", "tests.sim_runner", "--headless"])
        if rc != 0:
            print("\n[ERROR] Smoke test failed. Fix errors before building.")
            sys.exit(1)
        print("[OK] Tests passed.\n")
    else:
        print("[1/3] Smoke test skipped (--no-test).\n")

    # ── Patch spec for missing optional assets ────────────────────────────────
    no_icon    = not (root / "build_assets" / "simos_suite.ico").exists()
    no_version = not (root / "version_info.txt").exists()
    if no_icon or no_version:
        print("[2/3] Patching spec (missing assets):")
        if no_icon:    print("  - icon not found, removing from spec")
        if no_version: print("  - version_info.txt not found, removing from spec")
        active_spec = patch_spec(spec, no_icon, no_version)
    else:
        active_spec = spec
        print("[2/3] Spec OK — icon and version_info.txt found.\n")

    # ── Build ─────────────────────────────────────────────────────────────────
    print("[3/3] Building EXE...")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        str(active_spec),
        "--clean",
        "--noconfirm",
    ]
    if args.no_upx:
        cmd.append("--noupx")
    if args.debug:
        cmd += ["--debug", "all"]

    rc = run(cmd, cwd=root)
    if rc != 0:
        print("\n[ERROR] PyInstaller failed.")
        sys.exit(1)

    # ── Verify & report ───────────────────────────────────────────────────────
    exe = dist / "SimosSuite.exe"
    if not exe.exists():
        # Try without .exe (Linux/Mac)
        exe = dist / "SimosSuite"
    if exe.exists():
        size_mb = exe.stat().st_size / 1_048_576
        print(f"\n{'='*54}")
        print(f"  BUILD COMPLETE")
        print(f"  Output: {exe}")
        print(f"  Size:   {size_mb:.1f} MB")
        print(f"{'='*54}")
        print(f"\n  Test: {exe}")
        print(f"  Sim:  {exe} --ecu S85")
    else:
        print("\n[ERROR] Output EXE not found in dist/")
        sys.exit(1)


if __name__ == "__main__":
    main()
