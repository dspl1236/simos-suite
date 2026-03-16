# Simos Tuning Suite

A free, open-source ECU tuning, diagnostics, and right-to-repair platform for
Volkswagen Auto Group vehicles — built around the **Simos8.5 (3.0T TFSI, C7 A6/A7)**
with parallel support for Simos12/18 (2.0T EA888 family) and all four C7/MQB
transmission control units.

Companion repos:
- [esp32-isotp-ble-bridge-c7vag](https://github.com/dspl1236/esp32-isotp-ble-bridge-c7vag) — ESP32 firmware fork with C7 VAG CAN profile and raw sniff mode
- [VAG-CP-Docs](https://github.com/dspl1236/VAG-CP-Docs) — Component Protection research documentation

---

## Why this exists

Replacing a used HVAC module in your C7 A6 shouldn't require a dealer visit and
a live connection to VW's servers. This tool exists because vehicle owners have
the right to service their own property. Component Protection is documented
thoroughly in VAG-CP-Docs. This suite provides the tooling.

---

## License

**GPL v3** — free for personal use, free for right-to-repair advocacy, free forever.
Any modified version must also be open source. See [LICENSE](LICENSE).

Commercial use is not prohibited by the license, but this tool was not built for
shops charging $400 to run a 10-minute software operation. It was built for the
person sitting in the waiting room.

---

## What it does today

### Desktop GUI (`python -m ui`)

| Tab | Status | What it does |
|-----|--------|--------------|
| **Connect** | ✅ Live | Hardware interface selector — auto-detects ESP32 BLE/USB bridges by VID:PID (CP210x, CH340), scans J2534 DLL paths + Windows registry, SocketCAN on Linux. Manual COM port override. |
| **ECU Info** | ✅ Live | Reads 18 standard VW DIDs (VIN, part numbers, FAZIT, mileage, session state) from any connected ECU via UDS ReadDataByIdentifier. |
| **Flash** | ✅ Live | Full UDS CAL block flash: extended session → SA2 security access → erase (0xFF00) → RequestDownload → TransferData → RequestTransferExit → checksum verify (0xFF01). Progress bar + step indicators. Checksum auto-fix before write. |
| **Tune** | ✅ Live | Calibration table editor for all 14 Simos8.5 tables. Heat-map coloring, editable cells, correct RPM/load axis labels. 2D line chart for 1×N tables (MAF, throttle, boost limit). Lean diagnosis report. Fix checksums + save. |
| **Logger** | ✅ Live | Live DID poller — configurable channels, animating readout cards. |
| **CP Tools** | ✅ Live | J533 active DID probe — reads constellation list (0x04A3, 0x2A26–0x2A2C), IKA/GKA key DIDs (0x00BE, 0x00BD), full DID sweep. ODX parser — extracts CP routine ID, security level, SA2 bytecode, DID map from flashdaten .odx files. |
| **Raw Sniff** | ✅ Live | Hex dump of raw ISO-TP CAN frames from ESP32 bridge sniff mode (0xCAFE header). Use alongside ODIS to capture the CP removal UDS sequence. |
| **Trans** | ✅ Live | Transmission live data — ZF 8HP, DL501, DQ250-MQB, DQ381-MQB. Gear/selector/ATF temp/speed hero strip + full DID card grid. |

### Backend modules

| Module | What it does |
|--------|--------------|
| `core/ecu_defs.py` | ECU + TCU registry — block layout, SA2 scripts, crypto, CAN IDs for S85/SC1/SC2/SC8 + all four TCUs |
| `flasher/uds_flash.py` | UDS flash layer — works with BLE, USB, J2534, SocketCAN |
| `tuner/cal_parser.py` | Simos8.5 CAL parser — decode/edit all 14 tables, checksum fix, lean diagnosis |
| `transport/ble_bridge.py` | BLE transport — GATT 0xABF0, 8-byte header framing, split-packet reassembly, udsoncan-compatible |
| `transport/interfaces.py` | Interface registry — VID:PID auto-detect, J2534 DLL scan, SocketCAN |
| `cp_tools/j533_probe.py` | J533 active probe — constellation reads, full DID sweep, sniff mode |
| `cp_tools/odx_parser.py` | ODX XML parser — ASAM ODX 2.0, extracts CP routine and SA2 |
| `lib/connections/` | J2534 PassThru + USB ISO-TP connection implementations |
| `tests/mock_connection.py` | Simulated UDS device — `MockECU.SIMOS85 / J533 / J255 / ZF8HP / DQ250` |
| `tests/sim_runner.py` | Full GUI simulation harness — no hardware required |

---

## Supported hardware

| Interface | Type | OS | Notes |
|-----------|------|----|-------|
| **ESP32 BLE bridge** | `BLE` | Win/Mac/Linux | Wireless. Scan by GATT UUID 0xABF0. |
| **ESP32 USB bridge** | `USBISOTP` | Win/Mac/Linux | Same hardware, USB-C. Auto-detected by VID:PID. |
| **Tactrix OpenPort 2.0** | `J2534` | Windows | Recommended for large block flashes. |
| **Mongoose J2534** | `J2534` | Windows | Drew Tech / Bosch legacy cable. Works well. |
| **VNCI 6154A** | `J2534` | Windows | Clone ODIS cable. Good for probe/read. |
| **SocketCAN** | `SocketCAN_can0` | Linux | Requires iso-tp kernel module. |

---

## Supported ECUs

| Code | ECU | Engine | Platform | Status |
|------|-----|--------|----------|--------|
| S85 | Simos8.5 | 3.0T TFSI CGWA/B (C7 A6/A7) | PQ46 | ✅ Primary target |
| SC1 | Simos12 | 2.0T EA888 Gen1/2 | PQ46 | ✅ Defined |
| SC2 | Simos12.2 | 2.0T EA888 Gen3 | PQ46 | ✅ Defined |
| SC8 | Simos18.1/6 | 2.0T EA888 Gen3b MQB | MQB | ✅ Defined |

## Supported transmissions (live data)

| Code | TCU | Gearbox | Platform |
|------|-----|---------|----------|
| ZF8HP | Bosch | ZF 8-speed auto (C7 A6/A7/A8) | PQ46 |
| DL501 | Mechatronic | S-Tronic 7-speed DSG (C7 S6/S7) | PQ46 |
| DQ250 | Temic | 6-speed wet DSG (MQB Golf 7/Passat B8) | MQB |
| DQ381 | Bosch | 7-speed dry DSG (MQB Golf 8) | MQB |

---

## Quick start

```bash
# Clone
git clone https://github.com/dspl1236/simos-suite
cd simos-suite

# Install dependencies
pip install udsoncan python-can bleak pyserial numpy pycryptodome
pip install git+https://github.com/bri3d/sa2_seed_key.git

# Run in simulation mode (no hardware needed)
python -m tests.sim_runner

# Headless smoke test
python -m tests.sim_runner --headless

# Full GUI
python -m ui
```

---

## Component Protection research (Track A)

The last missing piece for offline CP research is the exact 2-byte RoutineControl
ID for `RoutiContrStartRoutiCompoProte` from `ES_LIBCompoProteGen3V12.sd.db`.

**Everything else is confirmed** from AU57X ODIS MCD project extraction:

| Item | Value | Source |
|------|-------|--------|
| Constellation DID | `0x04A3` | AU57X MWB extraction |
| Sub-DIDs (presence, sleep, DTC, allocation, TP-ID) | `0x2A26`–`0x2A2C` | AU57X MWB |
| IKA key DID (J533 + J255) | `0x00BE` — 34 bytes | AU57X MWB |
| GKA key DID (J255) | `0x00BD` — 34 bytes | AU57X MWB |
| CP activation flags | `0xEA61`–`0xEA64` | AU57X string DB |
| Security access for CP writes | None — extended session only | AU57X MWB |
| J255 ECU name code | `8` (Air Conditioning) | AU57X MWB |
| **CP routine ID** | **pending** — in `ES_LIBCompoProteGen3V12.sd.db` | — |

To extract: run `dumpMWB.py` (Linux-compatible via open-source PBL build) against
`ES_LIBCompoProteGen3V12.sd.db` from your ODIS-S installation.
See `technical/au57x-mcd-project-findings.md` in VAG-CP-Docs.

---

## Roadmap

- [x] Phase 1 — Foundation (ECU defs, CAL parser, UDS flash, BLE transport, J533 probe, ODX parser)
- [x] Phase 2 — GUI (all 8 tabs live, transmission live data, simulation harness)
- [ ] Phase 3 — CP automation (pending CP routine ID extraction)
- [ ] Phase 4 — Windows EXE (PyInstaller .spec, post smoke-test)
- [ ] Phase 5 — Android APK (Kotlin BLE client)

---

## Dependencies

```
udsoncan >= 1.21
python-can >= 4.0
bleak >= 0.21
numpy >= 1.24
pycryptodome
sa2_seed_key  (bri3d/sa2_seed_key)
pyserial >= 3.5
```

---

## Credits

- [bri3d/VW_Flash](https://github.com/bri3d/VW_Flash) — the foundation
- [Switchleg1/esp32-isotp-ble-bridge](https://github.com/Switchleg1/esp32-isotp-ble-bridge) — firmware
- [bri3d/sa2_seed_key](https://github.com/bri3d/sa2_seed_key) — SA2 bytecode interpreter
- [ConnorHowell/vag-uds-ids](https://github.com/ConnorHowell/vag-uds-ids) — VAG CAN ID table
- [VAG-CP-Docs](https://github.com/dspl1236/VAG-CP-Docs) — CP research

---

*Built for owners. GPL v3.*
