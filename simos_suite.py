"""
simos_suite.py — Single-file entry point for PyInstaller EXE build.

Run directly:
    python simos_suite.py
    python simos_suite.py --ecu SC8
    python simos_suite.py --debug

Built EXE:
    dist/simos_suite.exe  (Windows)
    dist/simos_suite      (Linux/macOS)
"""
from __future__ import annotations

import argparse
import logging
import sys


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Simos Tuning Suite — ECU/TCU diagnostics and right-to-repair",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  simos_suite.exe                     # Launch GUI (Simos8.5 default)
  simos_suite.exe --ecu SC8           # Launch with Simos18 / EA888 MQB
  simos_suite.exe --debug             # Verbose logging to console + file
  simos_suite.exe --headless          # Smoke-test: no GUI, exits 0/1
  simos_suite.exe --sim               # GUI in simulation mode (no hardware)

Supported ECUs  : S85  SC1  SC2  SC8
Supported TCUs  : ZF8HP  DL501  DQ250  DQ381
License         : GPL v3 — github.com/dspl1236/simos-suite
""")
    ap.add_argument("--ecu",       default=None,
                    help="Pre-select ECU key (S85, SC8, SC1, SC2)")
    ap.add_argument("--debug",     action="store_true",
                    help="Enable DEBUG-level logging")
    ap.add_argument("--headless",  action="store_true",
                    help="Headless smoke-test mode (no GUI) — exits 0=pass 1=fail")
    ap.add_argument("--sim",       action="store_true",
                    help="Simulation mode: no hardware required")
    ap.add_argument("--log-file",  default=None, metavar="PATH",
                    help="Write log output to file (default: auto in %%TEMP%%)")
    ap.add_argument("--version",   action="store_true",
                    help="Print version and exit")
    return ap.parse_args()


def _setup_logging(debug: bool, log_file: str | None) -> None:
    import os, pathlib, tempfile

    fmt  = "%(asctime)s  %(name)-28s  %(levelname)s  %(message)s"
    datefmt = "%H:%M:%S"
    level = logging.DEBUG if debug else logging.INFO

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    # Auto log file in TEMP when running as EXE
    if log_file is None and getattr(sys, "frozen", False):
        tmp = pathlib.Path(tempfile.gettempdir()) / "simos_suite.log"
        log_file = str(tmp)

    if log_file:
        try:
            fh = logging.FileHandler(log_file, encoding="utf-8", mode="a")
            fh.setFormatter(logging.Formatter(fmt, datefmt))
            handlers.append(fh)
        except OSError:
            pass

    logging.basicConfig(level=level, format=fmt, datefmt=datefmt,
                        handlers=handlers, force=True)


def _version() -> str:
    try:
        from ui.main_window import MainWindow
        return MainWindow.VERSION
    except Exception:
        return "0.1.0-alpha"


def main() -> int:
    args = _parse_args()

    if args.version:
        print(f"Simos Tuning Suite  v{_version()}")
        return 0

    _setup_logging(args.debug, args.log_file)
    log = logging.getLogger("SimosSuite")
    log.info("Simos Tuning Suite  v%s  Python %s", _version(), sys.version.split()[0])
    log.info("Frozen: %s", getattr(sys, "frozen", False))

    # ── Headless smoke-test ──────────────────────────────────────────────────
    if args.headless:
        from tests.sim_runner import run_headless
        ok = run_headless(args.ecu or "S85", "ZF8HP")
        return 0 if ok else 1

    # ── GUI modes ────────────────────────────────────────────────────────────
    if args.sim:
        log.info("Simulation mode — no hardware required")
        from tests.sim_runner import (
            _install_mock_patch, _install_interface_patch,
            auto_connect_after_launch,
        )
        _install_mock_patch(args.ecu or "S85", "ZF8HP")
        _install_interface_patch()

    # Tkinter check
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("ERROR: tkinter is not available on this Python installation.")
        print("       On Windows, reinstall Python and check 'tcl/tk' component.")
        return 2

    from ui.main_window import MainWindow
    app = MainWindow(ecu_key=args.ecu)

    if args.sim:
        auto_connect_after_launch(app, delay=1.5)

    log.info("Entering mainloop")
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
