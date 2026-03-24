# Simos Tuning Suite

Free, open-source ECU tuning, diagnostics, and right-to-repair tooling for
Volkswagen Auto Group vehicles.

**Primary target:** Simos8.5 — 3.0T TFSI CGWA/B (C7 A6/A7/A8)
**Also supported:** Simos12/18 (2.0T EA888), ZF 8HP, DL501, DQ250-MQB, DQ381-MQB

Companion repos:
- [esp32-isotp-ble-bridge-c7vag](https://github.com/dspl1236/esp32-isotp-ble-bridge-c7vag) — ESP32 BLE bridge firmware
- [VAG-CP-Docs](https://github.com/dspl1236/VAG-CP-Docs) — Component Protection research

---

## Download

**[SimosSuite.exe — latest](https://github.com/dspl1236/simos-suite/releases/latest)**  (Windows x64, ~30 MB)

Windows x64, single file, no install required. SmartScreen will flag an
unsigned binary on first run — click "More info" → "Run anyway". Source is
here, GPL v3.

---

## Why this exists

Replacing a used HVAC module in your C7 A6 requires a dealer visit and a live
connection to VW's servers — to re-authorize a $40 part you already own.
Component Protection is well-documented in VAG-CP-Docs. This suite provides the
tooling to do it yourself.

---

## License

**GPL v3.** Free for personal use, free for right-to-repair. Any modified
version must also be open source. See [LICENSE](LICENSE).

If you're a shop charging $400 to run a 10-minute software operation, this tool
is not for you — though the license doesn't stop you. The copyleft does.

---

## Feature status

| Tab | What it does |
|-----|-------------|
| **Connect** | Auto-detects BLE bridge (UUID 0xABF0), USB bridge (CP210x/CH340 VID:PID), J2534 DLL (registry scan), SocketCAN. Manual COM/path override. ECU selector for S85/SC1/SC2/SC8. |
| **ECU Info** | Reads 18 standard VW DIDs — VIN, part numbers, FAZIT, mileage, session state, ASAM file ID. |
| **Flash** | Full UDS CAL block flash: extended session → SA2 → erase (0xFF00) → RequestDownload → TransferData → exit → verify (0xFF01). Read CAL from ECU via ReadMemoryByAddress and auto-load into Tune tab. |
| **Tune** | Simos8.5 calibration editor — all 14 tables with real RPM/load axis labels. Heat-map coloring. Editable cells, color updates live. 2D chart for 1×N tables (MAF, throttle, limits). Lean diagnosis. Fix checksums + save. |
| **Logger** | Live DID poller using `logger.LogSession` — 16 channels, configurable interval, CSV export. |
| **CP Tools** | Full CP module scan — reads IKA key (DID 0x00BE) from all enrolled modules in one pass. J533 constellation probe, ODX parser. IKA key write + constellation update. CP routine ID 0x0226 wired in (pending hardware confirmation). |
| **Raw Sniff** | Passive CAN bus listener via J2534 raw CAN channel. Use with OBD splitter alongside VCDS/ODIS to capture full UDS exchanges. ISO-TP reassembly, UDS service decode, PCAP export for Wireshark. |
| **Trans** | ZF 8HP / DL501 / DQ250 / DQ381 live data — gear, selector, ATF temp, shaft speeds, torque, pressures. |

---

## Quick start

```bash
git clone https://github.com/dspl1236/simos-suite
cd simos-suite
pip install -r requirements.txt
pip install git+https://github.com/bri3d/sa2_seed_key.git

# No hardware? Run the full GUI in simulation mode:
python -m tests.sim_runner

# Headless backend test:
python -m tests.sim_runner --headless

# Real hardware:
python -m ui

# Passive CAN sniffer (OBD splitter + J2534):
# See Raw Sniff tab — captures VCDS/ODIS traffic without interfering
```

---

## Build the EXE yourself

```bat
git clone https://github.com/dspl1236/simos-suite
cd simos-suite
build.bat
```

`build.bat` installs all dependencies, runs the headless smoke test, and calls
PyInstaller. Output: `dist\SimosSuite.exe` (~70–90 MB).

Manual PyInstaller build:
```bash
pip install pyinstaller udsoncan bleak pyserial numpy pycryptodome python-can
pip install git+https://github.com/bri3d/sa2_seed_key.git
python build_exe.py
```

GitHub Actions builds the EXE on every tagged release (`v*`) and publishes
it to GitHub Releases automatically.

---

## Hardware

| Interface | Type | Notes |
|-----------|------|-------|
| ESP32 BLE bridge | `BLE` | [dspl1236/esp32-isotp-ble-bridge-c7vag](https://github.com/dspl1236/esp32-isotp-ble-bridge-c7vag). Scan by GATT UUID 0xABF0. |
| ESP32 USB bridge | `USBISOTP` | Same hardware, USB-C. Auto-detected by VID:PID (CP210x 10C4:EA60 or CH340 1A86:7523). |
| Tactrix OpenPort 2.0 | `J2534` | Recommended for block flashes. |
| Mongoose/Drew Tech | `J2534` | Works well. |
| VNCI 6154A | `J2534` | Good for read/probe. |
| SocketCAN | `SocketCAN_can0` | Linux only. Requires `iso-tp` kernel module. |

---

## ECU / TCU support

| Key | ECU | Engine | Platform |
|-----|-----|--------|----------|
| S85 | Simos8.5 | 3.0T TFSI CGWA/B (C7 A6/A7) | PQ46 — **primary target** |
| SC1 | Simos12 | 2.0T EA888 Gen1/2 | PQ46 |
| SC2 | Simos12.2 | 2.0T EA888 Gen3 | PQ46 |
| SC8 | Simos18.1/6 | 2.0T EA888 Gen3b | MQB |

| Key | TCU | Gearbox |
|-----|-----|---------|
| ZF8HP | Bosch | ZF 8-speed auto (C7 A6/A7/A8 D4) |
| DL501 | Mechatronic | S-Tronic 7-speed DSG (C7 S6/S7) |
| DQ250 | Temic | 6-speed wet DSG (MQB Golf 7/Passat B8) |
| DQ381 | Bosch | 7-speed dry DSG (MQB Golf 8) |

---

## Component Protection (Track A)

CP research is documented in [VAG-CP-Docs](https://github.com/dspl1236/VAG-CP-Docs).

**What is confirmed** (from AU57X ODIS MCD project + `ES_LIBCompoProteGen3V12.sd.db`):

| Item | Value | Status |
|------|-------|--------|
| Constellation DID | `0x04A3` | ✅ Confirmed |
| Sub-DIDs (presence, sleep, DTC, TP-ID) | `0x2A26`–`0x2A2C` | ✅ Confirmed |
| IKA key DID (J533 + J255) | `0x00BE` — 34 bytes | ✅ Confirmed |
| GKA key DID (J255 only) | `0x00BD` — 34 bytes | ✅ Confirmed |
| Security access for CP writes | None — extended session only | ✅ Confirmed |
| J255 ECU name code | `8` (Air Conditioning) | ✅ Confirmed |
| CP routine ID | **`0x0226`** (`31 01 02 26`) | ⏳ Pending hardware confirmation |

**To confirm `0x0226` on your car:**

```python
from cp_tools.j533_probe import J533Probe
probe = J533Probe(interface="BLE")
probe.connect()
resp = probe.start_cp_routine()
print(resp.hex() if resp else "no response")
# 0x7F 31 22 → conditions not correct = ID accepted, GEKO token required ✅
# 0x7F 31 31 → request out of range = wrong ID ❌
```

Once confirmed:
```bash
python -m cp_tools.mwb_extract --confirm 0x0226
```

---

## Roadmap

- [x] Phase 1 — Foundation (ECU defs, UDS flash, BLE transport, J533 probe, ODX parser)
- [x] Phase 2 — Full 8-tab GUI, trans live data, sim harness
- [x] Phase 3 — CP routine ID extracted (`0x0226`), logger wired, EXE build, passive CAN sniffer
- [ ] Phase 4 — CP hardware confirmation + full auth sequence
- [ ] Phase 5 — Android APK (Kotlin BLE client)

---

## Dependencies

```
udsoncan >= 1.21      # UDS protocol
bleak >= 0.21         # BLE transport
pyserial >= 3.5       # USB bridge
numpy >= 1.24         # CAL table math
pycryptodome >= 3.18  # AES for Simos12/18
python-can >= 4.0     # SocketCAN
sa2_seed_key          # pip install git+https://github.com/bri3d/sa2_seed_key.git
```

---

## Credits

- [bri3d/VW_Flash](https://github.com/bri3d/VW_Flash) — foundational reverse engineering
- [Switchleg1/esp32-isotp-ble-bridge](https://github.com/Switchleg1/esp32-isotp-ble-bridge) — ESP32 firmware
- [bri3d/sa2_seed_key](https://github.com/bri3d/sa2_seed_key) — SA2 bytecode interpreter
- [peterGraf/pbl](https://github.com/peterGraf/pbl) — PBL B-tree library (used for `.sd.db` extraction)
- [VAG-CP-Docs](https://github.com/dspl1236/VAG-CP-Docs) — CP research documentation

---

*Built for owners. GPL v3. github.com/dspl1236/simos-suite*
