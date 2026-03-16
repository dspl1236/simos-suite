"""
tests/ — Simos Suite test suite

Run all simulations without hardware:
    python -m tests.sim_ecu       ECU + flash + DID tests
    python -m tests.sim_trans     TCU live data + decode tests
    python -m tests               Run everything

No hardware required. No network. No filesystem writes.
"""
from __future__ import annotations
import sys
import os

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def run_all() -> bool:
    """Run all simulation test suites. Returns True if everything passes."""
    from tests import sim_ecu, sim_trans
    ok_ecu   = sim_ecu._print_results()
    ok_trans = sim_trans._print_results()
    return ok_ecu and ok_trans


if __name__ == "__main__":
    ok = run_all()
    sys.exit(0 if ok else 1)
