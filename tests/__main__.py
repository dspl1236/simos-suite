"""
tests/__main__.py — Run all simos-suite tests

    python -m tests            # run all test suites
    python -m tests --headless # skip GUI tests
    python -m tests --ecu SC8  # use Simos18 for sim tests
"""
from __future__ import annotations

import argparse
import sys


def main():
    ap = argparse.ArgumentParser(description="Simos Suite test runner")
    ap.add_argument("--headless", action="store_true",
                    help="Skip GUI simulation (run backend tests only)")
    ap.add_argument("--ecu",   default="S85",
                    help="ECU key for simulation (S85, SC8, SC1, SC2)")
    ap.add_argument("--trans", default="ZF8HP",
                    help="TCU key for simulation (ZF8HP, DQ250, DL501, DQ381)")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    import logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(name)-28s  %(levelname)s  %(message)s")

    all_pass = True
    print("\nSimos Tuning Suite — test runner")
    print("=" * 60)

    # 1. Headless smoke test (always runs)
    print("\n[1/3] smoke test (sim_runner --headless)")
    from tests.sim_runner import run_headless
    ok = run_headless(args.ecu, args.trans)
    all_pass = all_pass and ok

    # 2. ECU backend tests
    # sim_ecu runs all @_test-decorated functions at import time and
    # exposes _results + _print_results().
    print("\n[2/3] ECU backend tests (sim_ecu)")
    try:
        import tests.sim_ecu as sim_ecu
        ok = sim_ecu._print_results()   # returns True if all pass
        all_pass = all_pass and ok
    except Exception as e:
        print(f"  ERROR: {e}")
        all_pass = False

    # 3. Transmission tests
    print("\n[3/3] transmission tests (sim_trans)")
    try:
        import tests.sim_trans as sim_trans
        ok = sim_trans._print_results()
        all_pass = all_pass and ok
    except Exception as e:
        print(f"  ERROR: {e}")
        all_pass = False

    print("\n" + "=" * 60)
    print(f"Overall: {'PASS' if all_pass else 'FAIL'}")
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
