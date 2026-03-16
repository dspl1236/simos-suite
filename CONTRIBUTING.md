# Contributing to Simos Tuning Suite

This project was built for vehicle owners exercising their right to repair.
Contributions that advance that goal are welcome.

---

## What we need most

**1. ODX / flashdaten files**

The highest-impact contribution right now is `ES_LIBCompoProteGen3V12.sd.db`
from an ODIS-S installation. This unlocks the CP routine ID — the last missing
piece for Component Protection automation. See `cp_tools/mwb_extract.py`.

Other useful files:

| File | What it unlocks |
|------|-----------------|
| `EV_GatewPkoUDS_001_AU57.odx` | J533 CP routine ID, confirmed security level |
| `4G0907468*.sgo` or `.frf` | J533 C7 gateway firmware |
| `EV_AirCondiBasisUDS*.odx` | J255 2-zone HVAC DID map |
| `EV_AirCondiComfoUDS*.odx` | J255 4-zone HVAC DID map |
| `4G0820043*.sgo` | J255 firmware |

If you have ODIS-S installed, these live in:
```
C:\ProgramData\ODIS-S\diagdata\
C:\ProgramData\ODIS-S\PostSetup\
```

**2. Live UDS captures**

Packet captures from a real ODIS CP removal session, taken via the Raw Sniff
tab alongside ODIS. Even a single confirmed capture would close the loop on
the token format.

**3. Hardware testing**

The suite has been developed without constant hardware access. Reports of
what works (and what doesn't) with real ECUs, TCUs, and J2534 interfaces
are extremely valuable.

**4. A2L files**

Simos8.5 A2L files would let us confirm or correct the calibration table
offsets in `tuner/cal_parser.py`. The current offsets are community-derived.

---

## Code contributions

### Setup

```bash
git clone https://github.com/dspl1236/simos-suite
cd simos-suite
pip install -r requirements.txt
pip install git+https://github.com/bri3d/sa2_seed_key.git

# Verify everything works without hardware:
python -m tests.sim_runner --headless
```

### Running tests

```bash
# Headless smoke test (no hardware, no tkinter needed):
python -m tests.sim_runner --headless

# ECU-specific backend test:
python -m tests.sim_ecu

# Transmission test:
python -m tests.sim_trans

# Full GUI simulation:
python -m tests.sim_runner
```

### Areas needing work

- `flasher/uds_flash.py` — CAL read-back (`_do_read_cal` is a stub)
- `logger/` — LogSession is implemented; LoggerTab wiring needs live testing
- `cp_tools/j533_probe.py` — CP auth routine once routine ID is confirmed
- `tests/` — more coverage for edge cases (session timeouts, NRC handling)
- EXE build — `simos_suite.spec` for PyInstaller

### Code style

- Python 3.10+ with type hints
- `from __future__ import annotations` at top of every file
- `Menlo` / monospace font throughout the UI (already enforced)
- No third-party UI frameworks — tkinter + ttk only
- All backend functions must work with `MockConnection` for testability

### Submitting

Open a PR against `main`. Include:
- What you changed and why
- Whether you tested against real hardware
- Which interface you used (BLE, USB, J2534)

---

## License

By contributing, you agree your contributions are licensed under GPL v3.
See [LICENSE](LICENSE).

---

## What this is not for

This tool is for vehicle owners. If you're building a commercial CP removal
service on top of this, you are required by the GPL to release your modifications.
That is the only restriction.
