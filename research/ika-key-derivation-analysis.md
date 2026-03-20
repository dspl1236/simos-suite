# IKA Key Derivation Analysis

**Purpose:** Document the hardware test results (Scenario A/B/C) and any
patterns found in IKA key values across modules and vehicles.
**Status:** Awaiting hardware test — placeholder

---

## The open question

Does the IKA key `DID 0x00BE` (34 bytes) contain the same value across
all enrolled modules on a given vehicle, or is it unique per module?

**Scenario A** — All identical → VIN-bound derivation
- One blob fits all modules on this VIN
- Fix: write the Feb 2024 blob to any CP-active module
- Known blob: `E62B41D11C44AF202177FB1F274B0AC2D15BD262E4FD27AB61D123C2F15A2C932600`

**Scenario B** — Two variants → module-type-bound derivation
- Key depends on module category (seat vs HVAC vs gateway etc.)
- Fix: need one known blob per module type

**Scenario C** — All different → per-serial derivation
- Key is unique to each module's serial number
- Fix: requires GEKO-equivalent derivation algorithm

---

## Data collection template

Run ⟳ Scan All Modules with ignition on. Record results:

```
HARDWARE TEST
  Date:
  Vehicle: WAUGGA**********8 (primary)
  Ignition: ON / engine running

  MODULE IKA KEYS (DID 0x00BE)
  J533  Gateway:         _______________________________________________
  J255  Climatronic:     _______________________________________________
  J285  Instruments:     _______________________________________________
  J234  Airbag:          _______________________________________________
  J794  MMI:             _______________________________________________
  J136  Mem.Seat Driver: _______________________________________________
  J521  Mem.Seat Pass.:  _______________________________________________
  J518  KESSY:           _______________________________________________
  J519  Body Elect.:     _______________________________________________
  J525  Sound System:    _______________________________________________

  SCENARIO RESULT:  A / B / C
  Distinct blobs found: ___
  Matching the known Feb 2024 blob: YES / NO

  CONSTELLATION (0x04A3): _______________________________
```

---

## Analysis (complete after hardware test)

*This section will be filled in once scan results are available.*

---

*Once Scenario A is confirmed, this document will include the write procedure*
*and confirmation that the offline fix is fully determined.*
