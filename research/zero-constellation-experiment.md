# Zero Constellation Experiment

**Vehicle:** Spare 2013 Audi A6 C7 (identical platform to primary vehicle)
**Purpose:** Test whether writing all-zeros to J533 DID `0x04A3` disables
CP enforcement entirely, eliminating the need to write IKA keys per-module.
**Status:** Planned — not yet run

---

## Background

J533 enforces Component Protection by checking its constellation bitmap
(DID `0x04A3`, 10 bytes) against module identity responses on every
ignition cycle. The two-layer model is documented in VAG-CP-Docs.

The question: does J533 have a "virgin" or "CP disabled" state triggered
by a specific constellation value (e.g. all zeros)?

If yes — writing zeros disables CP enforcement system-wide without needing
to know any IKA keys. This would be a significantly simpler fix path.

**Known constellation values from Feb 2024 session:**
```
Before CP session:  FD A1 E9 0C FE 62 64 8D 00 00
After CP session:   FD A1 E8 0C FE 62 60 0D 00 00
```

Zero value to test: `00 00 00 00 00 00 00 00 00 00`

---

## Pre-test checklist

Before running the experiment, confirm the following:

- [ ] Spare C7 is running with at least one confirmed CP-active module
- [ ] Baseline scan run — note which modules are CP active vs clear
- [ ] Constellation DID `0x04A3` read and recorded (baseline value)
- [ ] IKA key DID `0x00BE` read from all modules — record all values
- [ ] Recovery value ready: `FD A1 E8 0C FE 62 60 0D 00 00`
      (write this back if zero causes issues)
- [ ] ESP32 bridge connected and confirmed working (scan passes)
- [ ] simos-suite connected via BLE or WiFi

---

## Test procedure

### Step 1 — Baseline scan
1. Ignition on
2. CP Tools tab → ⟳ Scan All Modules
3. Screenshot / note the verdict banner and all module statuses
4. Note the constellation value displayed

### Step 2 — Write zero constellation
1. CP Tools tab → ⊘ Try Zero Constellation
2. Confirm the dialog
3. Note the response:
   - `00 00 00 00 00 00 00 00 00 00` written and verified → proceed
   - NRC 0x22 → J533 requires token, zero rejected → record and stop
   - NRC 0x31 → zero not valid value → record and stop

### Step 3 — Ignition cycle (guided)
1. Use ⚡ Cycle Ignition button
2. Follow prompts: key off → 12s wait → key on → 10s wait
3. Auto-rescan fires after cycle

### Step 4 — Record result A: All modules clear
If verdict shows **✓ ALL MODULES CLEAR**:
- [ ] Record all module IKA key values (DID `0x00BE`) — did they change?
- [ ] Test module functionality physically (climate control, seat memory etc.)
- [ ] Run scan again after 10 minutes to confirm persistent
- [ ] Cycle ignition again and rescan — confirm survives second cycle
- [ ] Record: **zero constellation disables CP on C7 platform** ✓

### Step 5 — Record result B: Modules still CP active
If verdict shows **⚠ N MODULES CP ACTIVE**:
- [ ] Note which modules changed vs stayed the same
- [ ] Record: zero constellation does NOT disable CP enforcement
- [ ] Restore known-good constellation via ⊞ Update Constellation
- [ ] Confirm restore worked with rescan

### Step 6 — Record result C: Write rejected
If Step 2 returned an NRC:
- [ ] Record exact NRC code
- [ ] Record: J533 validates constellation structure before accepting write
- [ ] Note: zero is not a valid unauthenticated constellation value

---

## Additional experiments (if Step 4 passes)

If zero constellation works, test the following to understand the scope:

### ECU swap test
1. With zero constellation written and confirmed working
2. Physically swap a module (e.g. J255) from a different donor vehicle
3. Ignition cycle
4. Scan — does the swapped module show CP active?
   - If NO → zero constellation bypasses CP for new modules too (permanent bypass)
   - If YES → zero only affects currently-enrolled modules, new swaps still trigger CP

### Persistence test
1. With zero constellation confirmed working
2. Disconnect battery for 30+ minutes
3. Reconnect, ignition cycle
4. Scan — does zero constellation survive a battery disconnect?
   - Stored in MCU flash (not RAM) → should survive
   - If CP re-activates → something resets the constellation on power loss

### J533 swap test (advanced)
1. With zero constellation confirmed working on Spare C7
2. Install a different J533 from another donor vehicle
3. Ignition cycle
4. Scan — does a new J533 with no constellation also produce "all clear"?
   - This tests whether a blank J533 (virgin state) is inherently CP-free
   - Or whether zero specifically is the disable state

---

## Data capture template

Fill this in during/after the experiment:

```
Date:
Vehicle: Spare C7 (VIN WAUGGA**********x)
simos-suite version:
ESP32 firmware:
Transport: BLE / WiFi

BASELINE
  Constellation (0x04A3): _______________________
  Modules CP active: ____________________________
  Modules CP clear:  ____________________________

ZERO WRITE RESULT
  Write accepted:    YES / NO
  NRC if rejected:   0x___
  Readback value:    _______________________

POST-CYCLE SCAN
  Verdict:           ALL CLEAR / N CP ACTIVE
  Constellation:     _______________________
  Changed modules:   ____________________________

MODULE FUNCTIONALITY TEST
  J255 climate:      working / restricted / dead
  J136 seat:         working / restricted / dead
  J521 seat:         working / restricted / dead

CONCLUSION
  Zero disables CP:  YES / NO / PARTIAL
  Notes:
```

---

## Recovery procedure

If anything goes wrong, write the known-good constellation back:

1. CP Tools tab → ⊞ Update Constellation
2. This writes `FD A1 E8 0C FE 62 60 0D 00 00` to DID `0x04A3`
3. Ignition cycle
4. Rescan — should return to pre-experiment state

If Update Constellation also fails, the vehicle is in the same state as
any CP-active car. Standard IKA key write procedure applies.

---

*This experiment is safe to run — the recovery path is always available.*
*Run on the spare C7 only until results are confirmed.*
