# ECU Swap Test Protocol

**Purpose:** Structured protocol for documenting what happens when modules
are swapped between vehicles, how CP responds, and what the fix requires.
**Status:** Template — ready for use when spare C7 is available

---

## Why document swap behaviour

CP behaviour during a swap depends on:
- Whether the module was previously enrolled on any vehicle
- Whether the source and target constellations are compatible
- Whether the IKA key is VIN-bound or module-bound (Scenario A/B/C)
- Which generation of CP the module uses

Consistent documentation across multiple swaps builds the evidence base
for understanding the derivation and writing the automated fix.

---

## Per-swap data capture

For each module swap, record:

```
SWAP RECORD
  Date:
  Module:              J___ (description)
  Part number:         
  Source vehicle VIN:  WAUGGA**********x
  Target vehicle VIN:  WAUGGA**********x

  SOURCE MODULE STATE (before removal)
    DID 0x00BE (IKA key):   _______________________________
    CP status:              active / clear
    Enrolled in constellation: YES / NO

  TARGET VEHICLE STATE (before install)
    Constellation 0x04A3:   _______________________________
    Target module slot CP:  active / clear

  POST-INSTALL SCAN (ignition on, module installed)
    DID 0x00BE on new module: _____________________________
    Module CP status:         active / clear
    Constellation changed:    YES / NO
    New constellation:        _______________________________
    DTC codes stored:         _______________________________

  FIX APPLIED
    Method:  zero-const / IKA write / ODIS / none
    Result:  working / partially working / failed

  NOTES:
```

---

## Key questions to answer per swap

1. Does the module's IKA key survive physical removal and reinstall
   into the same vehicle? (Same IKA key before/after)

2. Does the module's IKA key change when installed in a different vehicle?
   (Checks whether key is stored in module EEPROM vs MCU flash)

3. Does J533 automatically detect a swapped module and update its
   constellation, or does it flag CP active immediately?

4. If zero constellation is already written — does a freshly installed
   module from another car trigger CP or does it work immediately?

---

## Module priority list for testing

| Priority | Module | Why |
|----------|--------|-----|
| 1 | J255 Climatronic | Primary affected module, well-understood |
| 2 | J136 Mem.Seat Driver | IKA blob known from Feb 2024 session |
| 3 | J521 Mem.Seat Pass. | Address unconfirmed — swap will confirm TX/RX IDs |
| 4 | J533 Gateway | Most destructive — test last, use known-good unit |
| 5 | J285 Instruments | CP active, cluster swap common community question |

---

*Results feed into `ika-key-derivation-analysis.md` once patterns emerge.*
