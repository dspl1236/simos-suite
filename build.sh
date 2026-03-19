#!/usr/bin/env bash
# build.sh — Simos Tuning Suite EXE builder (Linux/Mac cross-build or native)
#
# On Windows: use build.bat instead.
# On Linux/Mac: produces a native binary (not a .exe).
#   For a Windows .exe from Linux, use Wine + Windows Python, or GitHub Actions.

set -e

echo ""
echo "=== Simos Tuning Suite — build ==="
echo ""

# Dependencies
echo "[1/5] Installing dependencies..."
pip install --upgrade pyinstaller udsoncan bleak pyserial numpy pycryptodome python-can
pip install git+https://github.com/bri3d/sa2_seed_key.git || \
    echo "[WARN] sa2_seed_key install failed"

# Smoke test
echo "[2/5] Running headless smoke test..."
python -m tests.sim_runner --headless

mkdir -p build_assets build_hooks

echo "[3/5] Building..."
pyinstaller simos_suite.spec --clean --noconfirm

echo "[4/5] Checking output..."
if [[ "$OSTYPE" == "msys"* ]] || [[ "$OSTYPE" == "cygwin"* ]]; then
    OUT="dist/SimosSuite.exe"
else
    OUT="dist/SimosSuite"
fi

if [ -f "$OUT" ]; then
    SIZE=$(du -h "$OUT" | cut -f1)
    echo ""
    echo "=== BUILD COMPLETE ==="
    echo "  Output: $OUT  ($SIZE)"
    echo "  Test:   $OUT --ecu S85"
else
    echo "[ERROR] Output not found: $OUT"
    exit 1
fi
