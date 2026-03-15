# Simos Tuning Suite

A comprehensive open-source ECU tuning, flashing, and diagnostics platform for
Volkswagen Auto Group vehicles — built around the Simos ECU family with primary
focus on the **Simos8.5 (3.0T TFSI, C7 A6/A7)** and parallel support for
Simos12/18 (2.0T EA888 family).

Companion to [esp32-isotp-ble-bridge-c7vag](https://github.com/dspl1236/esp32-isotp-ble-bridge-c7vag)
and [VAG-CP-Docs](https://github.com/dspl1236/VAG-CP-Docs).

---

## Architecture

```
simos-suite/
│
├── core/
│   └── ecu_defs.py          ECU registry — FlashInfo, block layout, crypto,
│                             SA2 scripts, CAN IDs for every supported ECU
│
├── tuner/
│   └── cal_parser.py        CAL block parser — decode/edit calibration tables,
│                             checksum validation/fix, lean diagnosis helper
│
├── flasher/
│   └── uds_flash.py         UDS flash layer — connect, security access (SA2),
│                             erase, download, transfer, verify
│
├── cp_tools/
│   ├── j533_probe.py        J533 active DID probe — reads constellation data,
│   │                        compares J255 serial vs J533 constellation table
│   └── odx_parser.py        Flashdaten ODX parser — extracts CP routine ID,
│                             security level, SA2 script, full DID map
│
├── logger/
│   └── (next)               Live data logger — DID-based and ReadMemoryByAddress
│                             modes, configurable channel YAML, CSV/live gauge output
│
├── ui/
│   └── (next)               Desktop GUI — tabbed: ECU Info / Flash / Tune / Log /
│                             CP Tools / Raw Sniff
│
└── tests/
    └── (next)               Unit tests — checksum, crypto, ODX parsing, CAL decode
```

---

## Supported ECUs

| Code | ECU | Engine | Crypto | Status |
|------|-----|--------|--------|--------|
| S85  | Simos8.5  | 3.0T TFSI CGWA/B (C7 A6) | XOR | ✅ Primary target |
| SC1  | Simos12   | 2.0T EA888 Gen1/2        | AES | ✅ Defined |
| SC2  | Simos12.2 | 2.0T EA888 Gen3           | AES | ✅ Defined |
| SC8  | Simos18.1/6 | 2.0T EA888 Gen3b MQB   | AES | ✅ Defined |
| SCG  | Simos18.10 | 2.0T MQB Evo (Golf 8)   | AES | ✅ Defined |

---

## Simos8.5 (S85) — 3.0T TFSI Details

### Block layout
| Block | Name | Address | Size | Notes |
|-------|------|---------|------|-------|
| 1 | CBOOT | 0x80020000 | 80KB | Calibration bootloader |
| 2 | ASW1 | 0x80080000 | 1.5MB | Application software (single block) |
| 3 | CAL | 0xA0040000 | 240KB | **Calibration — all tunable tables** |

### Crypto
**XOR counter** — not AES. Each byte XOR'd with its position mod 256. Symmetric.
Discovered at 0x80017168 in 03F906070AK. This makes Simos8.5 the most accessible
ECU in the VW_Flash ecosystem for analysis.

### Lean condition / 3.2T block swap
The `cal_parser.py` `diagnose_lean()` method runs a structured check:
1. Lambda setpoint — are targets correctly stoichiometric?
2. MAF transfer function — does the air mass calibration match the installed sensor?
3. Injector scaling — does pulsewidth match the installed injector flow?

Common causes on the 3.0T / 3.2T block swap:
- If lean at ALL throttle positions: MAF transfer function mismatch (common if 3.2T
  intake manifold or different-diameter intake tract was installed)
- If lean at LIGHT THROTTLE only: lambda setpoint map issue, or O2 sensor
- If lean UNDER BOOST only: boost setpoint too aggressive, injector scaling too low

---

## Component Protection Research

See `cp_tools/` and [VAG-CP-Docs](https://github.com/dspl1236/VAG-CP-Docs).

### What we need from you

To fully unlock the CP protocol:

**Priority 1 (most critical):**
```
EV_GatewPkoUDS_001_AU57.odx     # J533 gateway ODX — contains CP routine ID
```

**Priority 2:**
```
4G0907468*.sgo or .frf          # J533 firmware flash container
EV_AirCondiBasisUDS*.odx        # J255 2-zone HVAC ODX
```

**Priority 3:**
```
EV_ECM30TFS*.odx or 4G0906259*.sgo    # Simos8.5 ECU ODX
```

Once the ODX is parsed, `cp_tools/odx_parser.py` extracts:
- CP routine ID → the exact `0x31 XX XX` bytes ODIS sends
- Security access level → SA2 challenge/response level for CP operations
- Token structure → what ODIS sends from the GRP server to J533

---

## Roadmap

### Phase 1 — Foundation ✅ (current)
- [x] ECU definitions registry (`core/ecu_defs.py`)
- [x] Simos8.5 CAL parser with lean diagnosis (`tuner/cal_parser.py`)
- [x] UDS flash layer for CAL block (`flasher/uds_flash.py`)
- [x] J533 active probe (`cp_tools/j533_probe.py`)
- [x] ODX parser for CP protocol extraction (`cp_tools/odx_parser.py`)
- [x] ESP32 BLE bridge fork with C7 VAG profile

### Phase 2 — Data capture
- [ ] Live logger with configurable YAML channels (`logger/`)
- [ ] ReadMemoryByAddress ($23) mode for Simos8 runtime values
- [ ] DID poll mode via persist/BLE bridge
- [ ] Log-to-CSV with configurable triggers (RPM threshold, WOT flag)
- [ ] AFR / lambda live channel (for confirming lean correction)

### Phase 3 — Tuner UI
- [ ] Desktop GUI (`ui/`) — Qt5 or tkinter
  - ECU Info tab (read VIN/serial/version)
  - Flash tab (read/write CAL block with checksum auto-fix)
  - Tune tab (table editor with color scaling, 2D/3D view)
  - Logger tab (live gauges + data log)
  - CP Tools tab (J533 probe + ODX viewer)
  - Raw Sniff tab (from BLE bridge companion)

### Phase 4 — CP automation (pending ODX)
- [ ] Parse CP routine ID and security level from ODX
- [ ] Document the GRP server protocol (capture during ODIS 25.x session)
- [ ] Implement J533 CP probe with correct DID addresses
- [ ] Publish CP removal UDS sequence for community reference

### Phase 5 — Android APK
- [ ] Port companion BLE client to Kotlin / Android
- [ ] Live data logging from phone
- [ ] CAL read/write via phone (backup + basic tune)

---

## Dependencies

```
udsoncan>=1.21
python-can>=4.0
bleak>=0.21       # BLE (for ESP32 bridge)
numpy>=1.24
pycryptodome      # AES for Simos12/18
sa2_seed_key      # SA2 seed/key (bri3d/sa2_seed_key)
```

---

## Related tools / credits

- [bri3d/VW_Flash](https://github.com/bri3d/VW_Flash) — the foundation this builds on
- [Switchleg1/esp32-isotp-ble-bridge](https://github.com/Switchleg1/esp32-isotp-ble-bridge) — hardware
- [bri3d/sa2_seed_key](https://github.com/bri3d/sa2_seed_key) — SA2 bytecode interpreter
- [ConnorHowell/vag-uds-ids](https://github.com/ConnorHowell/vag-uds-ids) — VAG CAN ID table
- [VAG-CP-Docs](https://github.com/dspl1236/VAG-CP-Docs) — CP research documentation
