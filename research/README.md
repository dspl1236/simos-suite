# simos-suite / research

Technical research notes, experimental test protocols, and findings from
hands-on work with VAG vehicles. This folder is for raw research — things
that are too specific, experimental, or tool-focused for the public-facing
VAG-CP-Docs advocacy repository.

## Contents

| File | Description |
|------|-------------|
| `zero-constellation-experiment.md` | Spare C7 test: does zeroing DID 0x04A3 disable CP enforcement? |
| `ika-key-derivation-analysis.md`   | Scenario A/B/C analysis once hardware test results are in |
| `ecu-swap-test-protocol.md`        | Structured protocol for documenting ECU swap CP behaviour |

## Relationship to VAG-CP-Docs

[VAG-CP-Docs](https://github.com/dspl1236/VAG-CP-Docs) is the public advocacy
repository — it documents consumer harm, legal context, and confirmed findings
for journalists, legislators, and repair shops.

This folder is where the raw experimental work lives. If something here
produces confirmed, reproducible results that serve the advocacy purpose,
a clean summary moves to VAG-CP-Docs. The detailed methodology and raw
data stays here.

---

## TTRS / TT 2.5T — Simos18.1 FRF Decryption (confirmed April 2026)

### Discovery
During TriCoreTool development, 8S0906259 (TTRS engine ECU) FRF files were
initially assumed to be MED17. Analysis revealed they are **Simos18.1** —
same platform as the EA888 2.0T Simos18 (SC8) but running the EA855 2.5T
5-cylinder engine.

### FRF Decryption Pipeline
```
FRF → XOR(frf.key, 4095 bytes) → ZIP → ODX(XML) → AES-128-CBC → LZSS → binary
```

- **AES key:** `98D31202E48E3854F2CA561545BA6F2F` (Simos18.1 — same as SC8)
- **AES IV:** `E7861278C508532798BCA4FE451D20D1`
- **FRF XOR key:** `data/frf.key` (4095 bytes, from bri3d/VW_Flash)

### Block Layout (Simos18.1 — TTRS)

| Block | Name | PFLASH Base | Binfile Offset | Size |
|-------|------|------------|----------------|------|
| 1 | CBOOT | 0x8001C000 | 0x01C000 | 130,560 bytes |
| 2 | ASW1 | 0x80040000 | 0x040000 | 916,480 bytes |
| 3 | ASW2 | 0x80140000 | 0x140000 | 1,047,552 bytes |
| 4 | ASW3 | 0x80880000 | 0x280000 | 1,309,696 bytes |
| 5 | CAL | 0xA0800000 | 0x200000 | 654,336 bytes |

### Verified FRF Files
All 8 TTRS engine FRFs decrypt successfully:
- 8S0906259B (3 revisions), 8S0906259C, J, N, R, _ (base)

### Compatible HW Part Numbers
`8S0906259`, `8S0906259B`, `8S0906259C`, `8S0906259J`, `8S0906259N`, `8S0906259R`

Companion ECU: `06K907425x` (TTRS secondary/knock controller — boxcode in FRF)

### EA855 vs EA888 Calibration Differences
The CAL block data structure differs between EA855 (5-cyl) and EA888 (4-cyl):
- No AOUV fingerprint `[14482, 609, 69, 130]` (EA855 has different oil pump calibration)
- Ignition map value ranges differ (EA855 uint16 distribution: 40.8% values >30000)
- Map dimensions should match FR database (KFZW = 12×16, KFNW = 12×12) but scaling differs
- Ghidra disassembly of ASW blocks needed to trace map references in 0xA0800000+ range

### Simos18.10 (SCG) — Different Key!
Also added from bri3d/VW_Flash `lib/modules/simos1810.py`:
- **AES key:** `AE540502E48E3854DBCA1A1545BA6F33` (DIFFERENT from 18.1!)
- **AES IV:** `62F313FA5C08532798BCA452471D20D5`
- Project code: SCG
- Larger CAL block: 0x9FC00 bytes at 0xA0820000
- Part: 5G0906259Q (Golf/A3 MQB Evo)

### Source
- FRF decryption algorithm: [bri3d/VW_Flash](https://github.com/bri3d/VW_Flash)
- Block layout: VW_Flash `lib/modules/simos18.py` and `simos1810.py`
- MED17 block header structure: [fanyi3315/bosch-med17-block-reader](https://github.com/fanyi3315/bosch-med17-block-reader)
