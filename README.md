# Simos Tuning Suite

An open-source toolkit for **reading, flashing, diagnosing, and researching** VW Auto
Group (Audi / VW) engine and transmission control units — built so an owner can service
their own car without a dealer, a live factory server, or a four-figure VCI.

It pairs a full UDS programming engine with a no-install GUI, bidirectional FRF/ODX/SGO
container codecs, and a Component-Protection (CP) research suite. It talks over cheap USB
hardware — an **ESP32 ISO-TP bridge** or the companion **CerberusCAN** board — and runs the
**entire GUI with zero hardware** via a built-in Simos8.5 simulator.

**Primary research target:** Simos8.5 — 3.0T TFSI (C7 A6/A7), Infineon TriCore TC1796
**Also defined:** Simos12 / 12.2 / 18.1 / 18.10 / TTRS · ZF 8HP · DL501 · DQ250 · DQ381

**Companion repos**
- [CerberusCAN](https://github.com/dspl1236/CerberusCAN) — Teensy 4.1 tri-CAN VCI + the TC1796 bench-BSL toolset
- [esp32-isotp-ble-bridge-c7vag](https://github.com/dspl1236/esp32-isotp-ble-bridge-c7vag) — ESP32 USB/BLE bridge firmware
- [VAG-CP-Docs](https://github.com/dspl1236/VAG-CP-Docs) — Component-Protection research writeups

---

## ⚠️ Status — read before you write

Built in the open, pre-1.0, **unsigned**, maturity labeled honestly throughout.

- **Read-only paths are mature and safe** — ECU info, logging, transmission live data, bus
  scan, DTC read, raw CAN sniff, and all offline container/checksum tooling.
- **The ECU/TCU *write* path is implemented but GUI-disabled** pending hardware validation.
  The full read → checksum → compress → encrypt → write pipeline exists end-to-end; the
  write buttons sit behind a banner.
- **CP write / routine actions are experimental and bench-gated** (the HVAC CP-patch flasher
  defaults to dry-run).

**Flashing can brick an ECU.** Don't run write features on a car you can't recover — have a
bench (boot-mode / BDM) setup and a verified known-good backup *first*. **No warranty** (GPL
§15–16); entirely at your own risk. The CP work is **research** on the author's own vehicle —
it does **not** defeat cryptographic signatures (see [Status & honesty](#status--honesty)).

---

## How the platforms actually reach (important)

Not every ECU flashes over OBD — and the suite is honest about which is which:

| Platform | Diagnose (read IDs, DTCs, live data) | Flash / full read |
|----------|--------------------------------------|-------------------|
| **Simos 18.x, DSG (DQ250/DQ381)** | OBD | **OBD** (programming session + SA2; signatures permitting) |
| **Simos 8.5, Simos 12** (TriCore TC1796/66) | OBD | **Bench / boot only** — OBD programming session is gated by the ECU |

So on the **Simos 8.5**: Simos-Suite reads its identity, DTCs and live data over OBD, but the
actual **read/write is a bench job through the TriCore CAN bootstrap loader** — see the
TC1796 BSL toolset in [CerberusCAN](https://github.com/dspl1236/CerberusCAN). The OBD flash
engine here is real and used for the Simos 18 / DSG families; for 8.x it's the bench path.

---

## Download

**[SimosSuite.exe — latest release](https://github.com/dspl1236/simos-suite/releases/latest)**
(Windows x64, single file, no install). It's **unsigned** — SmartScreen will flag it;
"More info → Run anyway" if you trust it, or run from source.

---

## Features

**Flash & read engine** (`flasher/uds_flash.py`) — extended → programming session, SA2
seed/key unlock, erase, RequestDownload/TransferData, exit+verify, driving the
checksum-fix → LZSS-compress → XOR(Simos8)/AES-CBC(Simos12+) pipeline. Virtual block read
(RequestUpload) + decrypt/decompress for the read → edit → reflash loop, where the ECU
allows it. *(Write GUI-disabled pending validation.)* Plus SA2-gated VIN read/write/verify.

**FRF / ODX / SGO containers** — FRF decrypt+extract (`frf_loader`), FRF repack (`frf_pack`,
per-block CRC32 recompute), the payload-codec matrix (RAW / Simos counter-XOR / Bosch-LZSS-AES,
`payload_codec`), a faithful VW LZSS port (`lzss_compress`), and SGO unpack + **byte-exact
repack** (`sgo_unpack` / `sgo_pack`, proven 69/69).

**Component-Protection research** *(right-to-repair, author's own car — see honesty note)* —
J533/J255 CP probe (`j533_probe`), a **proven** J533 gateway AES cipher model
(`gw_cp_cipher`, known-answer test closed), the HVAC IKA handshake model (`hvac_ika_cipher`,
bit-exactness bench-pending), bench-dump ingest, a dry-run-default HVAC CP-patch flasher, and
ODX/MWB analyzers.

**Checksums** — CRC32-VW, ECM3 64-bit summation validate/fix, Simos counter-XOR, and the
VW workshop-code generator.

**Logging & diagnostics** — threaded multi-channel DID logger (CSV export, gauge grid),
read-only transmission telemetry for all four TCUs, bus scan + DTC read/clear against a
~692-entry database, and a passive CAN sniffer with software ISO-TP reassembly + decode.

**CerberusCAN capture + decode** — drives the Teensy board as a request-level VCI and
reassembles a capture over **both ISO-TP and VW TP 2.0**, labeling UDS/KWP services and
flagging the CP-relevant ones (TrainICA, the `0x00BE` IKA write, SecurityAccess). Proven on a
real C7: gateway-routed comfort modules (seat, HVAC) decode on the 500k diag bus, so the
LS-FT comfort head isn't required.

**GUI** — single Tkinter app, dark theme, pure stdlib, **12 tabs** (Connect · Vehicle · ECU
Info · Flash · Flashware · Logger · CP Tools · CP Lab · CP Capture · Raw Sniff · Diagnostics ·
Trans). Ships as one unsigned EXE and runs the whole GUI with no hardware via the Simos8.5 sim.

---

## Install

```bash
git clone https://github.com/dspl1236/simos-suite && cd simos-suite
pip install -r requirements.txt
pip install git+https://github.com/bri3d/sa2_seed_key.git
```

Deps: `udsoncan`, `pyserial`, `python-can`, `numpy`, `pycryptodome`, + `sa2_seed_key` from
source. **Headless/offline tools** need only `numpy` + `sa2_seed_key`. Build the EXE with
`build.bat` (POSIX: `build.sh`) → `dist/SimosSuite.exe`.

---

## Quick start

```bash
python simos_suite.py --sim          # full GUI, no hardware (mock Simos8.5)
python simos_suite.py                # real hardware (auto-detects USB interfaces)
python simos_suite.py --headless     # backend smoke test
python -m tests                      # run all test suites

# A few offline CLI tools:
python -m flasher.frf_loader  path.frf --outdir out/    # decrypt FRF → flash blocks
python -m cp_tools.sgo_unpack file.sgo --out out/       # unpack an SGO container
python -m core.module_db      4G0820043                 # look up a C7 module
python -m cp_tools.gw_cp_cipher                          # J533 gateway cipher self-test (KAT)
```

`frf_pack`, `sgo_pack`, `checksum_simos`, `lzss_compress`, `vin_utils` are library APIs —
import them or drive via the test suite.

---

## Connections

This is a **USB-first** build. Select interfaces in the Connect tab; everything dispatches
through `flasher/uds_flash.py:_make_connection`.

| Interface | Key | Notes |
|-----------|-----|-------|
| ESP32 USB bridge | `USBISOTP` | ESP32 ISO-TP bridge over USB serial @250000. Auto-detected by VID/PID (CP2102 / CH340 / ESP32-S3). |
| CerberusCAN | `CERBERUS` | Teensy 4.1 tri-CAN (PJRC VID `0x16C0`), request-level VCI + live sniff/decode (CP Capture tab). Bus 1 = 500k diag. |
| Virtual mock | `MOCK` | Simos8.5 simulator — drives the whole GUI with no hardware. |
| Linux SocketCAN | `SocketCAN_<iface>` | Linux only (`iso-tp` kernel module). |

> Legacy **BLE / J2534 / WiFi** transports still exist in git history but are hidden in this
> build — they weren't part of the tested workflow and made the app look more capable than it
> is. Restore from git if you need them (J2534 needs a 32-bit Python; the EXE is x64-only).

---

## ECU / TCU support

| Key | ECU | Engine | Platform |
|-----|-----|--------|----------|
| `S85` | Simos8.5 | 3.0T TFSI (C7 A6/A7) | **primary** (bench/boot) |
| `SC1` / `SC2` | Simos12 / 12.2 | 2.0T EA888 Gen1/2 / Gen3 | PQ46 |
| `SC8` / `SC8_TTRS` | Simos18.1 | 2.0T EA888 Gen3b / 2.5T EA855 | MQB |
| `SCG` | Simos18.10 | 2.0T EA888 Gen3b Evo | MQB Evo |

TCUs: `ZF8HP` (C7 8-spd auto) · `DL501` (C7 S-tronic) · `DQ250` (MQB wet DSG) · `DQ381` (MQB
dry DSG). Definitions (SA2, block layouts, CAN IDs, crypto) live in `core/ecu_defs.py`; the
36-module C7 firmware inventory is in `data/c7_module_db.json`.

---

## Status & honesty

| Area | Maturity |
|------|----------|
| ECU info / VIN read · logging · trans live data · bus scan · DTC read+clear · CAN sniff/decode | **mature** |
| FRF decode · SGO unpack/repack (byte-exact) · LZSS · checksums | **mature** |
| Virtual block read (RequestUpload, where the ECU allows it) | working |
| **ECU/TCU flash WRITE** | **implemented, GUI-disabled** pending validation |
| J533 gateway CP cipher model | **proven** (KAT closed) |
| HVAC IKA model · CP routine / constellation / IKA write · HVAC CP-patch flasher | **modeled / experimental / bench-gated** |
| CerberusCAN capture + ISO-TP/TP 2.0 decode | **working** — proven on a real C7 capture |

**On Component Protection:** this is right-to-repair on the author's own car — e.g.
re-authorizing a used module you already own without a dealer's live server. The tooling reads
owner-writable records, models locally-checked symmetric ciphers, and patches firmware on the
author's own bench. **It does not defeat cryptographic signatures** — RSA/AES-signed modules
(BCM2, the J533 code blocks) are a wall and are not flashable or extractable here. Anything
unconfirmed is labeled so; bench reports and corrections are welcome.

---

## Credits

[bri3d/VW_Flash](https://github.com/bri3d/VW_Flash) (foundational VAG flash RE; `frf.key`) ·
[bri3d/sa2_seed_key](https://github.com/bri3d/sa2_seed_key) ·
[bri3d/TC1791_CAN_BSL](https://github.com/bri3d/TC1791_CAN_BSL) +
[fastboatster/TC1796_CAN_BSL](https://github.com/fastboatster/TC1796_CAN_BSL) (Simos bench/boot) ·
[Switchleg1/esp32-isotp-ble-bridge](https://github.com/Switchleg1/esp32-isotp-ble-bridge) ·
[peterGraf/pbl](https://github.com/peterGraf/pbl).

## License

**GPL-3.0** — free for personal use and right-to-repair; modifications stay open source. No
warranty (GPL §15–16).

---

*Built for owners. GPL-3.0 · github.com/dspl1236/simos-suite*
