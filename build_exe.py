"""
build_exe.py — Simos Suite EXE build helper

Usage:
    python build_exe.py                 # full build with smoke test
    python build_exe.py --no-test       # skip pre-build smoke test
    python build_exe.py --no-upx        # disable UPX (use in CI — no UPX installed)
    python build_exe.py --debug         # PyInstaller --debug all
"""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys


def run(cmd: list, **kw) -> int:
    print(f"\n$ {' '.join(str(c) for c in cmd)}")
    return subprocess.call(cmd, **kw)


def patch_spec(spec_path: pathlib.Path,
               no_icon: bool,
               no_version: bool,
               no_upx: bool) -> pathlib.Path:
    """
    Produce a patched .spec with missing optional assets removed.
    --noupx is NOT a valid PyInstaller flag when a .spec is passed,
    so UPX is disabled by rewriting upx=True to upx=False in the spec.
    """
    content = spec_path.read_text(encoding="utf-8")

    if no_icon:
        # Remove  icon='build_assets/simos_suite.ico'
        content = re.sub(r",\s*\n\s*icon\s*=\s*'[^']*'", "", content)
        print("  patched: removed icon reference (file not found)")

    if no_version:
        # Remove  version='version_info.txt'
        content = re.sub(r",\s*\n\s*version\s*=\s*'[^']*'", "", content)
        print("  patched: removed version reference (file not found)")

    if no_upx:
        # Flip upx=True -> upx=False inside the spec
        content = content.replace("upx=True", "upx=False")
        print("  patched: upx=True -> upx=False (--no-upx)")

    patched = spec_path.parent / "simos_suite_patched.spec"
    patched.write_text(content, encoding="utf-8")
    return patched


def main():
    ap = argparse.ArgumentParser(description="Simos Suite EXE builder")
    ap.add_argument("--no-test",  action="store_true",
                    help="Skip pre-build headless smoke test")
    ap.add_argument("--no-upx",   action="store_true",
                    help="Disable UPX compression (patch spec, not PyInstaller flag)")
    ap.add_argument("--debug",    action="store_true",
                    help="Pass --debug all to PyInstaller")
    args = ap.parse_args()

    root = pathlib.Path(__file__).parent
    spec = root / "simos_suite.spec"

    if not spec.exists():
        print(f"[ERROR] simos_suite.spec not found in {root}")
        sys.exit(1)

    # ── 1. Pre-build smoke test ───────────────────────────────────────────────
    if not args.no_test:
        print("\n[1/3] Running headless smoke test...")
        rc = run([sys.executable, "-m", "tests.sim_runner", "--headless"])
        if rc != 0:
            print("\n[ERROR] Smoke test FAILED. Fix errors before building.")
            sys.exit(1)
        print("[OK] Tests passed.")
    else:
        print("[1/3] Smoke test skipped (--no-test).")

    # ── 2. Patch spec for missing/unwanted assets ─────────────────────────────
    no_icon    = not (root / "build_assets" / "simos_suite.ico").exists()
    no_version = not (root / "version_info.txt").exists()
    needs_patch = no_icon or no_version or args.no_upx

    print("\n[2/3] Preparing spec...")
    if needs_patch:
        active_spec = patch_spec(spec, no_icon, no_version, args.no_upx)
    else:
        active_spec = spec
        print("  spec OK — no patching needed")

    # ── 3. Build ──────────────────────────────────────────────────────────────
    print("\n[3/3] Building EXE with PyInstaller...")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        str(active_spec),
        "--clean",
        "--noconfirm",
    ]
    if args.debug:
        cmd += ["--debug", "all"]
    # NOTE: never add --noupx here — not valid with a .spec file.
    #       UPX is controlled via upx=True/False inside the spec.

    rc = run(cmd, cwd=root)
    if rc != 0:
        print("\n[ERROR] PyInstaller failed. See output above.")
        sys.exit(1)

    # ── Verify ────────────────────────────────────────────────────────────────
    dist = root / "dist"
    # Check all possible output names (spec name controls this)
    for candidate in ["SimosSuite.exe", "simos_suite.exe", "SimosSuite", "simos_suite"]:
        exe = dist / candidate
        if exe.exists():
            break

    if exe.exists():
        size_mb = exe.stat().st_size / 1_048_576
        print(f"\n{'='*54}")
        print(f"  BUILD COMPLETE")
        print(f"  Output : {exe}")
        print(f"  Size   : {size_mb:.1f} MB")
        print(f"{'='*54}")
        print(f"\n  Run:  {exe}")
        print(f"  Sim:  {exe} --ecu S85")
    else:
        print("\n[ERROR] Output EXE not found — check PyInstaller output above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
