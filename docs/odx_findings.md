# ODX Extraction Findings

Extracted from `Flashdaten_Audi_20201020` and `Flashdaten_Volkswagen_20201020`
using VW_Flash `frf/decryptfrf.py`. All FRF containers use the same XOR+ZIP
encryption scheme. Date: March 2026.

---

## J533 Lear Gateway — 4H0907468E / 4H0907468AC

**SA2 script (CONFIRMED, identical in both files):**
```
6805814A05870A22128A494C
```
Verified executable — seed `0x12345678` → key `0x52CEEA16`

**Compatible source HW (EXPECTED-IDENTS from 0204 ODX):**
```
A8 D4:    4H0907468C, D, E
A6/A7 C7: 4G0907468, A, B, C  ← your car is in this list
```
Same firmware runs across both A8 D4 and A6/A7 C7 — confirmed.

**Block layout:** Block 01 = 483,328 bytes (firmware), Block 03 = 2,048 bytes (config)

**Important:** These are FLASH ODX files. CP routine IDs and DID map
are in the MCD Runtime Projects — use kartoffelpflanze/ODIS-project-explorer
against `MCD-Projects-E/VWMCD/AU57X/`.

---

## J255 Climatronic — 4G0820043H (4-zone) / 4G0820043L (2-zone)

**SA2 script (CONFIRMED, identical for both variants):**
```
93270319464C
```
Verified — seed `0x12345678` → key `0x39376FBE`. Only 6 bytes.

**Block layout:** Block 01 = 741,376 bytes, Block 03 = 2,048 bytes

**Compatible HW:**
- 4-zone (H): `4G0820043, A, E, F, G, H, M, N`
- 2-zone (L): `4G0820043B, C, D, J, K, L`

---

## Simos8 — 03F906070KA (VW reference, XOR crypto confirmed)

**SA2:** `6803824A10680284443932244A05872709200481499384251648824A058712082001824A0181494C`

**Encrypt-compress method: `0x11` = XOR counter + LZSS — CONFIRMED**

Block layout: CBOOT=81,408B / ASW=1,702,400B / CAL=245,760B (matches S85)

---

## 4G0906014F — C7 TDI diesel ECU (NOT the 3.0T TFSI)

6-block layout = Bosch EDC17. The 3.0T TFSI part is `4G0906259x` — not in
this flashdaten set. Simos8.5 SA2 in VW_Flash is still valid for S85.

---

## FRF Format

Magic: `0A 9C 92 7C ...` — XOR cipher + ZIP. Decrypt with `VW_Flash/frf/decryptfrf.py`.
