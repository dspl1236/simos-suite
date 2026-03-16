"""
logger/channels_s85.py — Simos8.5 (3.0T TFSI CGWB) channel presets

Pre-built Channel sets for the most common logging scenarios on the
C7 A6/A7 3.0T TFSI. Import and pass directly to LogSession.

Usage
─────
    from logger.channels_s85 import CHANNELS_FUEL, CHANNELS_BOOST, CHANNELS_FULL
    from logger import LogSession

    session = LogSession(ecu=SIMOS85, interface="BLE",
                         channels=CHANNELS_BOOST)

Channel sets
────────────
    CHANNELS_ESSENTIAL   Battery, RPM, throttle, coolant — always-on baseline
    CHANNELS_FUEL        Fueling: MAF, lambda B1/B2, STFT/LTFT, injector PW
    CHANNELS_BOOST       Boost: MAP, boost setpoint, wastegate DC, throttle
    CHANNELS_IGNITION    Spark: advance B1/B2, knock retard B1/B2, STFT
    CHANNELS_LEAN_DIAG   Everything needed for lean condition diagnosis
    CHANNELS_FULL        All channels — use at 500ms+ interval

DID addresses
─────────────
These are community-confirmed for Simos8.5 CGWB in extended session.
Some addresses may differ on other CAL versions. Verify against your
DID 0xF19E (ASAM file ID) if you get unexpected NRC 0x31 responses.

Source: community UDS scans, simos_hsl.py (bri3d/VW_Flash), VCDS
measurement block cross-reference.
"""

from __future__ import annotations
from logger import Channel

# ── Essential — always poll these ─────────────────────────────────────────────

CHANNELS_ESSENTIAL = [
    Channel(0xF442, "Battery",    "V",   0.001,  0.0,  2, False, "{:.2f}"),
    Channel(0xF186, "Session",    "",    1.0,    0.0,  1, False, "{:#04x}"),
    Channel(0x2000, "RPM",        "rpm", 0.25,   0.0,  2, False, "{:.0f}"),
    Channel(0x2006, "Throttle",   "%",   0.1,    0.0,  2, False, "{:.1f}"),
    Channel(0x2008, "Coolant",    "°C",  0.5,  -40.0,  2, False, "{:.1f}"),
    Channel(0x295A, "Mileage",    "km",  1.0,    0.0,  4, False, "{:.0f}"),
]

# ── Fueling channels ──────────────────────────────────────────────────────────

CHANNELS_FUEL = CHANNELS_ESSENTIAL + [
    Channel(0x2002, "MAF",        "g/s",  0.01,  0.0,  2, False, "{:.2f}"),
    Channel(0x2004, "Lambda B1",  "λ",    0.001, 0.0,  2, False, "{:.3f}"),
    Channel(0x2010, "Lambda B2",  "λ",    0.001, 0.0,  2, False, "{:.3f}"),
    Channel(0x2005, "Inj PW",     "ms",   0.004, 0.0,  2, False, "{:.2f}"),
    Channel(0x200B, "LTFT B1",    "%",    0.01,-100.0,  2, False, "{:.1f}"),
    Channel(0x200C, "STFT B1",    "%",    0.01,-100.0,  2, True,  "{:.1f}"),
    Channel(0x200D, "LTFT B2",    "%",    0.01,-100.0,  2, False, "{:.1f}"),
    Channel(0x200E, "STFT B2",    "%",    0.01,-100.0,  2, True,  "{:.1f}"),
    Channel(0x2003, "IAT",        "°C",   0.5, -40.0,  2, False, "{:.1f}"),
]

# ── Boost / pressure channels ─────────────────────────────────────────────────

CHANNELS_BOOST = CHANNELS_ESSENTIAL + [
    Channel(0x2001, "MAP",        "kPa",  0.1,   0.0,  2, False, "{:.1f}"),
    Channel(0x2011, "MAP SP",     "kPa",  0.1,   0.0,  2, False, "{:.1f}"),
    Channel(0x2012, "Boost abs",  "bar",  0.001, 0.0,  2, False, "{:.3f}"),
    Channel(0x2013, "Boost SP",   "bar",  0.001, 0.0,  2, False, "{:.3f}"),
    Channel(0x2014, "WG duty",    "%",    0.392, 0.0,  1, False, "{:.1f}"),
    Channel(0x2006, "Throttle",   "%",    0.1,   0.0,  2, False, "{:.1f}",
            enabled=False),   # already in essential, skip duplicate
    Channel(0x2015, "Turbo RPM",  "rpm",  10.0,  0.0,  2, False, "{:.0f}"),
    Channel(0x2002, "MAF",        "g/s",  0.01,  0.0,  2, False, "{:.2f}"),
    Channel(0x2007, "Torque req", "Nm",   0.5,   0.0,  2, True,  "{:.0f}"),
]

# ── Ignition / knock channels ─────────────────────────────────────────────────

CHANNELS_IGNITION = CHANNELS_ESSENTIAL + [
    Channel(0x200A, "Ign adv B1", "°",    0.1,   0.0,  2, True,  "{:.1f}"),
    Channel(0x2016, "Ign adv B2", "°",    0.1,   0.0,  2, True,  "{:.1f}"),
    Channel(0x2017, "Knock B1",   "°",    0.1,   0.0,  2, True,  "{:.1f}"),
    Channel(0x2018, "Knock B2",   "°",    0.1,   0.0,  2, True,  "{:.1f}"),
    Channel(0x2019, "Knock ret1", "°",    0.1,   0.0,  2, True,  "{:.1f}"),
    Channel(0x201A, "Knock ret2", "°",    0.1,   0.0,  2, True,  "{:.1f}"),
    Channel(0x200C, "STFT B1",    "%",    0.01,-100.0,  2, True,  "{:.1f}"),
    Channel(0x2000, "RPM",        "rpm",  0.25,  0.0,  2, False, "{:.0f}",
            enabled=False),  # already in essential
]

# ── Lean diagnosis — all channels needed to isolate the cause ─────────────────
# See also: CalParser.diagnose_lean() for offline static analysis

CHANNELS_LEAN_DIAG = [
    Channel(0xF442, "Battery",    "V",    0.001,  0.0,  2, False, "{:.2f}"),
    Channel(0x2000, "RPM",        "rpm",  0.25,   0.0,  2, False, "{:.0f}"),
    Channel(0x2002, "MAF",        "g/s",  0.01,   0.0,  2, False, "{:.2f}"),
    Channel(0x2003, "IAT",        "°C",   0.5,  -40.0,  2, False, "{:.1f}"),
    Channel(0x2004, "Lambda B1",  "λ",    0.001,  0.0,  2, False, "{:.3f}"),
    Channel(0x2010, "Lambda B2",  "λ",    0.001,  0.0,  2, False, "{:.3f}"),
    Channel(0x200B, "LTFT B1",    "%",    0.01,-100.0,  2, False, "{:.1f}"),
    Channel(0x200C, "STFT B1",    "%",    0.01,-100.0,  2, True,  "{:.1f}"),
    Channel(0x200D, "LTFT B2",    "%",    0.01,-100.0,  2, False, "{:.1f}"),
    Channel(0x200E, "STFT B2",    "%",    0.01,-100.0,  2, True,  "{:.1f}"),
    Channel(0x2005, "Inj PW",     "ms",   0.004,  0.0,  2, False, "{:.2f}"),
    Channel(0x2001, "MAP",        "kPa",  0.1,    0.0,  2, False, "{:.1f}"),
    Channel(0x2006, "Throttle",   "%",    0.1,    0.0,  2, False, "{:.1f}"),
    Channel(0x2008, "Coolant",    "°C",   0.5,  -40.0,  2, False, "{:.1f}"),
    # 3.0T specific: these tell you if MAF transfer or injector scaling is the issue
    Channel(0x201B, "Load calc",  "mg",   0.1,    0.0,  2, False, "{:.1f}"),
    Channel(0x201C, "Load meas",  "mg",   0.1,    0.0,  2, False, "{:.1f}"),
    Channel(0x201D, "Inj cor B1", "%",    0.01,-100.0,  2, True,  "{:.1f}"),
    Channel(0x201E, "Inj cor B2", "%",    0.01,-100.0,  2, True,  "{:.1f}"),
]

# ── Full channel set — use at 500ms+ interval ─────────────────────────────────

CHANNELS_FULL = [
    # Identity
    Channel(0xF190, "VIN",         "",    1.0,    0.0, 17, False, "{}"),
    Channel(0xF442, "Battery",     "V",   0.001,  0.0,  2, False, "{:.2f}"),
    Channel(0xF186, "Session",     "",    1.0,    0.0,  1, False, "{:#04x}"),
    Channel(0x295A, "Mileage",     "km",  1.0,    0.0,  4, False, "{:.0f}"),
    # Engine
    Channel(0x2000, "RPM",         "rpm", 0.25,   0.0,  2, False, "{:.0f}"),
    Channel(0x2001, "MAP",         "kPa", 0.1,    0.0,  2, False, "{:.1f}"),
    Channel(0x2002, "MAF",         "g/s", 0.01,   0.0,  2, False, "{:.2f}"),
    Channel(0x2003, "IAT",         "°C",  0.5,  -40.0,  2, False, "{:.1f}"),
    Channel(0x2004, "Lambda B1",   "λ",   0.001,  0.0,  2, False, "{:.3f}"),
    Channel(0x2005, "Inj PW",      "ms",  0.004,  0.0,  2, False, "{:.2f}"),
    Channel(0x2006, "Throttle",    "%",   0.1,    0.0,  2, False, "{:.1f}"),
    Channel(0x2007, "Torque req",  "Nm",  0.5,    0.0,  2, True,  "{:.0f}"),
    Channel(0x2008, "Coolant",     "°C",  0.5,  -40.0,  2, False, "{:.1f}"),
    Channel(0x2009, "Oil temp",    "°C",  0.5,  -40.0,  2, False, "{:.1f}"),
    Channel(0x200A, "Ign adv B1",  "°",   0.1,    0.0,  2, True,  "{:.1f}"),
    Channel(0x200B, "LTFT B1",     "%",   0.01,-100.0,  2, False, "{:.1f}"),
    Channel(0x200C, "STFT B1",     "%",   0.01,-100.0,  2, True,  "{:.1f}"),
    Channel(0x200D, "LTFT B2",     "%",   0.01,-100.0,  2, False, "{:.1f}"),
    Channel(0x200E, "STFT B2",     "%",   0.01,-100.0,  2, True,  "{:.1f}"),
    Channel(0x200F, "Fuel pres",   "bar", 0.01,   0.0,  2, False, "{:.2f}"),
    Channel(0x2010, "Lambda B2",   "λ",   0.001,  0.0,  2, False, "{:.3f}"),
    Channel(0x2011, "MAP SP",      "kPa", 0.1,    0.0,  2, False, "{:.1f}"),
    Channel(0x2012, "Boost abs",   "bar", 0.001,  0.0,  2, False, "{:.3f}"),
    Channel(0x2013, "Boost SP",    "bar", 0.001,  0.0,  2, False, "{:.3f}"),
    Channel(0x2014, "WG duty",     "%",   0.392,  0.0,  1, False, "{:.1f}"),
    Channel(0x2016, "Ign adv B2",  "°",   0.1,    0.0,  2, True,  "{:.1f}"),
    Channel(0x2017, "Knock B1",    "°",   0.1,    0.0,  2, True,  "{:.1f}"),
    Channel(0x2018, "Knock B2",    "°",   0.1,    0.0,  2, True,  "{:.1f}"),
]

# ── Named presets for the Logger tab dropdown ─────────────────────────────────

PRESETS = {
    "essential":   ("Essential",         CHANNELS_ESSENTIAL),
    "fuel":        ("Fueling",           CHANNELS_FUEL),
    "boost":       ("Boost / pressure",  CHANNELS_BOOST),
    "ignition":    ("Ignition / knock",  CHANNELS_IGNITION),
    "lean_diag":   ("Lean diagnosis",    CHANNELS_LEAN_DIAG),
    "full":        ("Full (500ms+)",     CHANNELS_FULL),
}
