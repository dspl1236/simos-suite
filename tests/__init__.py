"""
tests/ — Simos Suite test and simulation package

Modules
───────
  mock_connection.py   — udsoncan-compatible MockConnection + SimulatedECU/TCU
  sim_runner.py        — Full GUI simulation harness + headless smoke test

Quick start
───────────
  # Headless smoke test (no tkinter needed):
  python -m tests.sim_runner --headless

  # Full GUI in simulation mode (no hardware needed):
  python -m tests.sim_runner
  python -m tests.sim_runner --ecu SC8 --trans DQ250
  python -m tests.sim_runner --ecu DL501

  # Just the mock connection in your own code:
  from tests.mock_connection import MockConnection, SimulatedECU, SimulatedTCU
  from core.ecu_defs import SIMOS85
  conn = MockConnection(SimulatedECU(SIMOS85))
"""
