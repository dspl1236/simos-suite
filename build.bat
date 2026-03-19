@echo off
REM ─────────────────────────────────────────────────────────────────────────────
REM build.bat — Simos Tuning Suite Windows EXE builder
REM
REM Run from the repo root with Python 3.10+ in PATH:
REM     build.bat
REM
REM Output: dist\SimosSuite.exe
REM ─────────────────────────────────────────────────────────────────────────────

setlocal enabledelayedexpansion

echo.
echo  ╔══════════════════════════════════════════════════════╗
echo  ║        Simos Tuning Suite — EXE Build Script        ║
echo  ╚══════════════════════════════════════════════════════╝
echo.

REM Check Python
python --version 2>NUL
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+ and ensure it's in PATH.
    exit /b 1
)

REM Check pip
pip --version 2>NUL
if errorlevel 1 (
    echo [ERROR] pip not found.
    exit /b 1
)

echo [1/6] Installing / updating core dependencies...
pip install --upgrade ^
    pyinstaller ^
    udsoncan ^
    bleak ^
    pyserial ^
    numpy ^
    pycryptodome ^
    python-can ^
    || goto :error

echo.
echo [2/6] Installing sa2_seed_key (SA2 bytecode interpreter)...
pip install git+https://github.com/bri3d/sa2_seed_key.git || (
    echo [WARN] sa2_seed_key install failed - flash security access may not work
)

echo.
echo [3/6] Running headless smoke test (pre-build verification)...
python -m tests.sim_runner --headless
if errorlevel 1 (
    echo [ERROR] Smoke test FAILED. Fix the errors above before building.
    exit /b 1
)
echo [OK] All tests passed.

echo.
echo [4/6] Creating build assets directory...
if not exist build_assets mkdir build_assets
if not exist build_hooks mkdir build_hooks

REM Copy hooks from repo
copy /Y build_hooks\hook-bleak.py build_hooks\ 2>NUL
copy /Y build_hooks\hook-udsoncan.py build_hooks\ 2>NUL
copy /Y build_hooks\hook-sa2_seed_key.py build_hooks\ 2>NUL

REM Generate a placeholder icon if none exists
if not exist build_assets\simos_suite.ico (
    echo [INFO] No icon found at build_assets\simos_suite.ico
    echo        Place a 256x256 ICO file there for a custom icon.
    echo        Building without icon...
    REM Remove icon line from spec if no icon present
    python -c "
import re, pathlib
spec = pathlib.Path('simos_suite.spec').read_text()
spec = re.sub(r\",\s*\n\s*icon='build_assets/simos_suite.ico'\", '', spec)
spec = re.sub(r\",\s*\n\s*version='version_info.txt'\", '', spec)
pathlib.Path('simos_suite.spec').write_text(spec)
print('Spec patched: icon and version removed (files not found)')
" 2>NUL
)

echo.
echo [5/6] Building EXE with PyInstaller...
pyinstaller simos_suite.spec --clean --noconfirm
if errorlevel 1 goto :error

echo.
echo [6/6] Verifying output...
if not exist dist\SimosSuite.exe (
    echo [ERROR] dist\SimosSuite.exe not found - build may have failed.
    goto :error
)

for %%A in (dist\SimosSuite.exe) do set SIZE=%%~zA
set /a SIZE_MB=!SIZE! / 1048576
echo.
echo  ╔══════════════════════════════════════════════════════╗
echo  ║                   BUILD COMPLETE                     ║
echo  ╠══════════════════════════════════════════════════════╣
echo  ║  Output:  dist\SimosSuite.exe                        ║
echo  ║  Size:    !SIZE_MB! MB                              ║
echo  ╚══════════════════════════════════════════════════════╝
echo.
echo  Test it:  dist\SimosSuite.exe
echo  Sim mode: dist\SimosSuite.exe --ecu S85
echo.
goto :end

:error
echo.
echo [ERROR] Build failed. See output above.
exit /b 1

:end
endlocal
