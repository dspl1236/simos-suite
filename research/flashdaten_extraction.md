# Flashdaten Extraction Report

**Source**: Flashdaten_Audi_20201020_6ZtF7, Flashdaten_Volkswagen_20201020_op5fy
**Date**: 2026-03-25
**Tool**: flasher/frf_loader.py (FrfLoader)
**Total Audi FRF files scanned**: 5,078

---

## 1. SA2 Script Extraction Summary

### 1.1 Simos8.5 ECU (4G0907551) -- 13 FRF files, 1 unique SA2

**SA2 (40 bytes)**:
```
6805824A10680493300419624A05871510197082499324041966824A058702031970824A0181494C
```

All 13 files produce the identical SA2 bytecode:
- FL_4G0907551A__0005.frf through FL_4G0907551___0008.frf
- Variants: A, D, E, F, G, J, and bare (no suffix)

**ecu_defs.py match**: YES -- SIMOS85.sa2_script matches exactly.

**EXPECTED-IDENT part numbers from ODX**:
| File | Part Number | SW Version |
|------|------------|------------|
| 4G0907551A__0005 | 4G0907551A | 0001 |
| 4G0907551A__0006 | 4G0907551A | 0001 |
| 4G0907551A__0010 | 4G0907551A | 0001 |
| 4G0907551D__0006 | 4G0907551D | 0001 |
| 4G0907551E__0003 | 4G0907551E | 0001 |
| 4G0907551F__0004 | 4G0907551  | 0001 |
| 4G0907551F__0005 | 4G0907551  | 0001 |
| 4G0907551F__0006 | 4G0907551  | 0001 |
| 4G0907551G__0003 | 4G0907551G | 0001 |
| 4G0907551G__0005 | 4G0907551G | 0001 |
| 4G0907551J__0004 | 4G0907551J | 0001 |
| 4G0907551___0007 | 4G0907551  | 0001 |
| 4G0907551___0008 | 4G0907551  | 0001 |

**Block layout (confirmed, all variants identical)**:
- Block 1 (CBOOT/PBL): 81,408 bytes (0x13E00) -- encrypt-method: 0x01 (XOR)
- Block 2 (ASW1): 1,572,352 bytes (0x17FE00) -- encrypt-method: 0x01 (XOR)
- Block 3 (CAL): 261,632 bytes (0x3FE00) -- encrypt-method: 0x01 (XOR)
- Checksum: CRC32 on all three blocks

---

### 1.2 J533 Gateway (4H0907468) -- 7 FRF files, 1 unique SA2

**SA2 (12 bytes)**:
```
6805814A05870A22128A494C
```

All 7 files produce identical SA2:
- FL_4H0907468AB_0028_S.frf, FL_4H0907468AC_0037_S.frf
- FL_4H0907468D__0183_S.frf, FL_4H0907468E__0202_S.frf
- FL_4H0907468E__0204.frf, FL_4H0907468E__0204_S.frf
- FL_4H0907468F__0213_S.frf

**CRITICAL BUG IN ecu_defs.py**: The J533_LEAR.sa2_script is **WRONG**. It currently contains the Simos8.5 SA2 (40 bytes) instead of the actual J533 gateway SA2 (12 bytes).

| | Value |
|---|---|
| **ecu_defs.py J533_LEAR** (WRONG) | `6805824A10680493300419624A05871510197082499324041966824A058702031970824A0181494C` |
| **Correct J533 SA2** (from flashdaten) | `6805814A05870A22128A494C` |

The comment block at line 149 correctly states "Both return identical SA2: 6805814A05870A22128A494C" but the actual bytes.fromhex() on line 176 uses the Simos8.5 value. The conflicting comment on line 172 says "SA2 script CONFIRMED from FL_4G0907551D__0006.frf" which is a Simos8.5 ECU file.

**EXPECTED-IDENT part numbers from ODX**:
| File | Part Number | SW Version | HW Version |
|------|------------|------------|------------|
| 4H0907468E__0204 | 4H0907468C | 0156 | -- |
| 4H0907468D__0183_S | 4H0907468C | 0156 | -- |
| 4H0907468F__0213_S | 4H0907468C | 0156 | -- |
| 4H0907468AC_0037_S | 4H0907468AA | X011 | H05 |
| 4H0907468AB_0028_S | 4H0907468AA | X011 | H05 |

Note: The compatible_hw list in ecu_defs.py includes 4G0907468 family entries. These are NOT present as EXPECTED-IDENT values in any of the ODX files examined. The ODX only contains 4H0907468x entries. The 4G0 entries in the compatible_hw list may have been inferred from other sources (the same firmware runs on both A6 C7 and A8 D4).

**Block layout (two generations found)**:

Generation 1 (4H0907468C/D/E/F, SW 0156-0213):
- Block 01 (FIRMWARE): 483,328 bytes (0x76000)
- Block 03 (CONFIG): 2,048 bytes (0x800)
- Encrypt: 0x00 (none)
- Signature: SIG_SHA1-RSA1024 (on _S suffix files) or SIG_SHA1-RSA1024_S

Generation 2 (4H0907468AA/AB/AC, SW X011+):
- Block 01 (FIRMWARE): 983,040 bytes (0xF0000) -- **2x larger**
- Block 03 (CONFIG): 65,536 bytes (0x10000) -- **32x larger**
- Encrypt: 0x00 (none)
- Signature: SIG_SHA1-RSA1024_S

The ecu_defs.py binfile_size=983040 matches Generation 2, but the block sizes {1: 0x76000, 3: 0x800} match Generation 1. This is inconsistent.

---

### 1.3 J255 Climatronic HVAC (4G0820043) -- 24 FRF files, 1 unique SA2

**SA2 (6 bytes)**:
```
93270319464C
```

All 24 files produce identical SA2, across all variants:
- H (4-zone HIGH original): 3 files
- HI (4-zone HIGH international): 9 files
- L (2-zone LOW original): 3 files
- LO (2-zone LOW international): 7 files
- R (unknown variant): 2 files

**ecu_defs.py match**: YES -- J255_4ZONE.sa2_script and J255_2ZONE.sa2_script both match.

**Block layout**:

H/L variants (original, SW 0056-0065):
- Block 01 (FIRMWARE): 741,376 bytes (0xB5400) -- matches ecu_defs.py
- Block 03 (CONFIG): 2,048 bytes (0x800) -- matches ecu_defs.py

HI/LO variants (international, SW 0076-0810):
- Block 01 (FIRMWARE): 1,003,520 bytes (0xF5000) -- **larger than ecu_defs.py**
- Block 03 (CONFIG): 2,048 bytes (0x800)

R variant (SW 0031-0032):
- Block 01 (FIRMWARE): 1,003,520 bytes (0xF5000) -- same as HI/LO
- Block 03 (CONFIG): 2,048 bytes (0x800)
- Signature: SIG_SHA1-RSA1024_S (RSA-signed)

**EXPECTED-IDENT**:
- H variant: 4G0820043 (HW H08) -- note: bare part number, no suffix
- L variant: 4G0820043B (HW H09)
- R variant: no EXPECTED-IDENT found

**New finding**: The R variant (4G0820043R) is not listed in ecu_defs.py compatible_hw lists and has a larger firmware block (0xF5000 vs 0xB5400). Same SA2 script though.

---

### 1.4 ZF 8HP TCU (0BH300) -- 49 FRF files, 1 unique SA2

**SA2 (40 bytes)**:
```
6805824A10680284100819734A05872506200382499318111973824A058712082001824A0181494C
```

All 49 files produce identical SA2 across all sub-part numbers (0BH300011N through 0BH300047).

**ecu_defs.py match**: YES -- ZF8HP.sa2_script matches exactly.

**Block layout (all variants identical)**:
- Block 02 (ASW/Firmware): 1,015,808 bytes (0xF8000)
- Block 03 (CAL/Data): 131,072 bytes (0x20000)
- Checksum: CRC32 on blocks 2 and 3
- Encrypt: 0x00 (none); one variant (0BH300012J) uses 0x10 (compressed)

**Complete part number list from EXPECTED-IDENTS**:
```
0BH300011N, 0BH300011P, 0BH300011Q, 0BH300011S, 0BH300011T
0BH300012A, 0BH300012B, 0BH300012C, 0BH300012D, 0BH300012J
0BH300012K, 0BH300012Q, 0BH300012R, 0BH300012S
0BH300046A, 0BH300046K
0BH300047
```

17 unique ZF 8HP part numbers confirmed compatible.

---

### 1.5 TDI ECU (4G0906014) -- 22 FRF files, 1 unique SA2

**SA2 (34 bytes)**:
```
680693600919814A0893658792AA826B058782AD16124A078420FC80916B0181494C
```

All 11 tested files produce identical SA2 (Bosch EDC17 family).

**ecu_defs.py match**: NOT IN ecu_defs.py. This is the C7 3.0 TDI diesel ECU.

**Block layout (6-block EDC17)**:
- Block 01: 48,896 bytes (0xBF00) -- bootloader
- Block 02: 1,179,648 bytes (0x120000) -- ASW1
- Block 03: 786,432 bytes (0xC0000) -- ASW2
- Block 04: 786,432 bytes (0xC0000) -- ASW3
- Block 05: 786,432 bytes (0xC0000) -- ASW4
- Block 06: 507,904 bytes (0x7C000) -- CAL
- Encrypt: 0x11 (XOR + LZSS) on data blocks, 0x00 on erase blocks

---

### 1.6 Simos8 VW (03F906070) -- 27 FRF files, 1 unique SA2

**SA2 (40 bytes)**:
```
6803824A10680284443932244A05872709200481499384251648824A058712082001824A0181494C
```

**ecu_defs.py match**: NOT IN ecu_defs.py as a standalone entry. Note: SIMOS12.sa2_script is `6803814A10680393290720094A05872212195482499309011953824A058730032009824A0181494C` which is different (Simos12, not Simos8).

The 03F906070 SA2 is specific to the VW-branded Simos8 (1.8T/2.0T EA113/888 Gen1) and differs from the Audi 4G0907551 Simos8.5 (3.0T TFSI) SA2.

---

### 1.7 4H0907472A (Unknown Module) -- 2 FRF files, 1 unique SA2

**SA2 (12 bytes)**:
```
6805814A05870a221289494c
```

Nearly identical to J533 gateway SA2 (`6805814A05870A22128A494C`) -- differs only in byte 9: 0x89 vs 0x8A.

**Block layout**:
- Block 30: 1,569 bytes (0x621) -- very small, possibly config/parameter
- Block 00: 90,112 bytes (0x16000) -- main firmware
- Encrypt: 0x00 (none)
- Checksum: CRC32

This is likely another Lear Electronics module (possibly instrument cluster or comfort module) given the SA2 similarity to J533.

---

### 1.8 FDC Gateway (FDC8W7907468B) -- 1 FRF file, 1 unique SA2

**SA2 (92 bytes)**:
```
814A0A8400000001871EDC6F41814A0A8400000001871EDC6F41814A07871EDC6F416B059300000001
814A0A8400000001871EDC6F41814A0A8400000001871EDC6F41814A0A8400000001871EDC6F41
814A0A8400000001871EDC6F41814A07871EDC6F416B059300000001680784803614234A0687
0400020082494C
```

This is the MQB/MLB Evo gateway (8W5907468B, Audi A4/A5 B9 family). Completely different SA2 structure from the PQ46/C7 J533.

**Block layout (5 data blocks)**:
- Block 01: 128 bytes (erase/program routine)
- Block 02: 3,047,408 bytes (0x2E7FF0) -- main firmware
- Block 03: 189,572 bytes (0x2E484) -- calibration
- Block 04: 16,653,559 bytes (0xFE1CF7) -- large data block
- Block 05: 8,202,082 bytes (0x7D2762) -- large data block
- Block 06: 127,979 bytes (0x1F3EB)
- Encrypt: 0x0A (AES) and 0x50
- Ident: 8W5907468B

---

## 2. VW Flashdaten TCU Extraction

### 2.1 DQ250 (0D9300xxx) -- found in VW Flashdaten

**SA2 (31 bytes)**:
```
68028149680593A55A55AA4A0587810595268249845AA5AA558703F780384C
```

**ecu_defs.py match**: YES -- DQ250.sa2_script matches exactly.

**Block layout**:
- Block 30 (DRIVER): 2,062 bytes (0x80E), compressed to 1,206 bytes
- Block 50 (ASW): 1,245,184 bytes (0x130000), compressed to ~374K
- Block 51 (CAL): 131,072 bytes (0x20000), compressed to ~25K
- Encrypt: 0x11 (XOR/LZSS compression)
- Checksum: CRC32 on blocks 2, 3, 4

Note: Block numbers use 30/50/51 scheme (not 2/3/4 as in VW_Flash documentation).

### 2.2 DQ381 / 0GC300xxx -- found in VW Flashdaten

**SA2 (12 bytes)**:
```
6806814A05876B5F7DD5494C
```

**ecu_defs.py match**: YES -- DQ381.sa2_script matches exactly.

**Block layout**:
- Block 01 (BOOT): 130,560 bytes (0x1FE00), compressed to ~100K
- Block 02 (ASW): 1,113,600 bytes (0x10FE00), compressed to ~880K
- Block 03 (CAL): 261,632 bytes (0x3FE00), compressed to ~58K
- Encrypt: 0xAA (AES-CBC confirmed from VW_Flash)
- Checksum: CRC32

### 2.3 DL501 (0B5300xxx) -- NOT FOUND

No DL501 FRF files found in any of the six brand directories (Audi, VW, Bentley, Lamborghini, Seat, Skoda). The DL501 S-Tronic is likely only available through GEKO/SVM online programming, not in offline flashdaten sets.

---

## 3. ODX Deep Parse Results

### 3.1 J533 Gateway ODX Analysis

The J533 flash ODX files contain **only flash data** -- no DIAG-COMM elements, no DID definitions, no RoutineControl services, and no adaptation channels. This is expected: the diagnostic layer ODX (containing DID/routine definitions) is a separate file distributed through VW's ODIS online system, not bundled in flash containers.

**What was found**:
- SA2 script: `6805814A05870A22128A494C` (confirmed across 7 files)
- ALFID: `014101` (Address and Logical Format Identifier for RequestDownload)
- RSA signatures: SHA1-RSA1024 on _S suffix files (signed flash containers)
- No CP-related DIDs, RoutineControl IDs, or writable DIDs in flash ODX

**Implication**: To get J533 diagnostic service definitions (CP DIDs, adaptation channels, RoutineControl), you need the DIAG ODX from ODIS, or must reverse-engineer them via UDS scanning.

### 3.2 J255 HVAC ODX Analysis

Same situation as J533 -- flash-only ODX with no diagnostic service definitions.

**What was found**:
- SA2 script: `93270319464C` (confirmed across 24 files)
- RSA signatures on _S files
- No CP-related DIDs or writable DIDs in flash ODX

### 3.3 ZF 8HP TCU ODX Analysis

Flash-only ODX, no diagnostic services embedded.

**What was found**:
- SA2 script: `6805824A10680284100819734A05872506200382499318111973824A058712082001824A0181494C` (confirmed across 49 files)
- CRC32 checksums on blocks 2 and 3
- No VIN/marriage/IMMO/coding DID definitions
- No RoutineControl (factory reset) definitions
- No WriteDataByIdentifier services
- No adaptation channels

---

## 4. ecu_defs.py Discrepancy Report

### 4.1 CRITICAL: J533 SA2 Script is WRONG

**File**: `core/ecu_defs.py`, line 176
**Current value** (WRONG -- this is the Simos8.5 SA2):
```python
sa2_script = bytes.fromhex("6805824A10680493300419624A05871510197082499324041966824A058702031970824A0181494C")
```

**Correct value** (from 7 independent J533 ODX files):
```python
sa2_script = bytes.fromhex("6805814A05870A22128A494C")
```

The comment block above the definition (line 149) correctly documents `6805814A05870A22128A494C` but the actual code uses the Simos8.5 value. The conflicting comment on line 172 ("SA2 script CONFIRMED from FL_4G0907551D__0006.frf") is the source of the error -- 4G0907551D is a Simos8.5 ECU, not a gateway.

### 4.2 J533 Block Layout Inconsistency

ecu_defs.py defines:
- binfile_size = 983,040 (matches Gen2 4H0907468AA/AB/AC)
- Block 1: 0x76000 = 483,328 (matches Gen1 4H0907468C/D/E/F)
- Block 3: 0x800 = 2,048 (matches Gen1)

This mixes Gen1 block sizes with Gen2 binfile_size. Should pick one:

**Gen1** (4H0907468C through F, SW 0156-0213):
- Block 01: 483,328 bytes (0x76000)
- Block 03: 2,048 bytes (0x800)
- Total: ~485,376

**Gen2** (4H0907468AA/AB/AC, SW X011+):
- Block 01: 983,040 bytes (0xF0000)
- Block 03: 65,536 bytes (0x10000)
- Total: ~1,048,576

### 4.3 J533 compatible_hw List Needs Update

Current ecu_defs.py lists:
```python
compatible_hw = ["4H0907468C", "4H0907468D", "4H0907468E",
                 "4G0907468", "4G0907468A", "4G0907468B", "4G0907468C"]
```

From the flashdaten ODX EXPECTED-IDENT fields, only 4H0907468C and 4H0907468AA are confirmed as target part numbers. The 4G0907468 family entries were not found as EXPECTED-IDENT values (though they may still be hardware-compatible -- the same PCB design is used).

Additional part numbers from the FRF filenames that should be added:
```
4H0907468D, 4H0907468E, 4H0907468F (Gen1)
4H0907468AA, 4H0907468AB, 4H0907468AC (Gen2)
```

### 4.4 J255 HI/LO and R Variants Missing

ecu_defs.py J255_4ZONE compatible_hw lists H-suffix variants, but the flashdaten also contains:
- **HI** variants (international 4-zone, 9 FRF files): larger firmware block (0xF5000 vs 0xB5400)
- **LO** variants (international 2-zone, 7 FRF files): larger firmware block
- **R** variant (2 FRF files): same larger block, RSA-signed

The R variant is not in any compatible_hw list. The HI/LO variants may need separate BlockDef entries due to different firmware sizes.

### 4.5 Simos8 VW (03F906070) SA2 Not in ecu_defs.py

The VW-specific Simos8 (03F906070 family, 1.8T/2.0T) has a different SA2 from both the Audi Simos8.5 (4G0907551) and the Simos12 (which is currently defined):

```
03F906070 SA2: 6803824A10680284443932244A05872709200481499384251648824A058712082001824A0181494C
```

This could be added as a SIMOS8 entry if VW 1.8T/2.0T support is desired.

### 4.6 TDI ECU (4G0906014) SA2 Not in ecu_defs.py

The C7 3.0 TDI Bosch EDC17 ECU SA2 is available:
```
680693600919814A0893658792AA826B058782AD16124A078420FC80916B0181494C
```

6-block layout (typical EDC17), encrypt method 0x11 (XOR+LZSS).

### 4.7 DQ250 Block Number Mapping

VW_Flash documents DQ250 blocks as 2/3/4 (DRIVER/ASW/CAL), but the flashdaten ODX uses block numbers 30/50/51. This mapping should be noted.

---

## 5. New Data Available for Future Use

### 5.1 All Confirmed SA2 Scripts (Complete Table)

| Module | Part Number Family | SA2 Hex | Length | In ecu_defs.py? |
|--------|-------------------|---------|--------|-----------------|
| Simos8.5 | 4G0907551x | `6805824A10680493300419624A05871510197082499324041966824A058702031970824A0181494C` | 40 bytes | YES (correct) |
| J533 Gateway | 4H0907468x | `6805814A05870A22128A494C` | 12 bytes | **WRONG** (has Simos8.5) |
| J255 HVAC | 4G0820043x | `93270319464C` | 6 bytes | YES (correct) |
| ZF 8HP TCU | 0BH300xxx | `6805824A10680284100819734A05872506200382499318111973824A058712082001824A0181494C` | 40 bytes | YES (correct) |
| DQ250 DSG | 0D9300xxx | `68028149680593A55A55AA4A0587810595268249845AA5AA558703F780384C` | 31 bytes | YES (correct) |
| DQ381 DSG | 0GC300xxx | `6806814A05876B5F7DD5494C` | 12 bytes | YES (correct) |
| Simos8 VW | 03F906070x | `6803824A10680284443932244A05872709200481499384251648824A058712082001824A0181494C` | 40 bytes | No |
| TDI EDC17 | 4G0906014x | `680693600919814A0893658792AA826B058782AD16124A078420FC80916B0181494C` | 34 bytes | No |
| Lear Unknown | 4H0907472A | `6805814A05870a221289494c` | 12 bytes | No |
| FDC Gateway | 8W5907468B | (92-byte script, see section 1.8) | 92 bytes | No |

### 5.2 ZF 8HP Complete Part Number Registry

17 unique ZF 8HP TCU part numbers confirmed from flashdaten EXPECTED-IDENT:
```
0BH300011N    0BH300012A    0BH300012K    0BH300046A
0BH300011P    0BH300012B    0BH300012Q    0BH300046K
0BH300011Q    0BH300012C    0BH300012R    0BH300047
0BH300011S    0BH300012D    0BH300012S
0BH300011T    0BH300012J
```

Sub-families:
- 0BH300011x: Earlier generation (SW 1411-5250)
- 0BH300012x: Main production run (SW 2212-5254)
- 0BH300046x: Variant (possibly different application, SW 1812-2275)
- 0BH300047: Latest (SW 5206-5221)

### 5.3 Encryption Methods Found

| Module | Encrypt-Compress-Method | Meaning |
|--------|------------------------|---------|
| Simos8.5 | 0x01 | XOR counter (no compression) |
| J533 Gateway | 0x00 | None |
| J255 HVAC | 0x00 | None |
| ZF 8HP | 0x00 (0x10 for 012J) | None (012J: LZSS compressed) |
| TDI EDC17 | 0x11 | XOR counter + LZSS |
| DQ250 | 0x11 | XOR counter + LZSS |
| DQ381 | 0xAA | AES-CBC |
| FDC Gateway | 0x0A / 0x50 | AES / unknown |

### 5.4 RSA Signature Information

Files with _S suffix contain SHA1-RSA1024 digital signatures. The RSA public key modulus varies by module family. These signatures are checked by the bootloader during flash and cannot be bypassed without bootloader modification or RSA key extraction.

Modules with RSA signatures observed:
- J533 Gateway (all _S files)
- J255 HVAC (all _S files)
- J255 R variant (all files)
- FDC Gateway

---

## 6. Limitations of Flash ODX Files

The Flashdaten FRF containers only include **flash-layer ODX** files. These contain:
- Flash block data (firmware binaries)
- SA2 seed/key bytecode
- ALFID (Address Logical Format ID)
- Block checksums (CRC32)
- EXPECTED-IDENT (target part numbers)
- RSA signatures (where applicable)
- Encryption method codes

They do **NOT** contain:
- DIAG-COMM elements (diagnostic service definitions)
- DID (Data Identifier) definitions
- RoutineControl definitions
- WriteDataByIdentifier service definitions
- Adaptation channel definitions
- Component Protection (CP) related data structures

To obtain diagnostic-layer ODX data (DID definitions, CP DIDs, RoutineControl, adaptation channels), you need:
1. The DIAG ODX files from VW's ODIS diagnostic system (separate download)
2. UDS brute-force scanning of the live ECU
3. Community-sourced DID lists (e.g., from VCDS long coding databases)
