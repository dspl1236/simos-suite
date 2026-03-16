# Simos8.5 3.0T TFSI — Tuning Reference

*Platform: Audi C7 A6/A7, engine codes CGWA / CGWB / CGWC*  
*This guide covers what each calibration table does, safe operating ranges,
and how to diagnose common issues on the 3.0T TFSI using the simos-suite
tune tab and live logger.*

---

## Engine overview

The 3.0T TFSI (CGWB) is a supercharged 2,995cc V6. Unlike the turbocharged
EA888 engines, boost is produced by a Roots-type TVS supercharger driven
directly off the crankshaft via a belt. This means:

- Boost response is near-instant — no turbo lag
- Boost pressure is a function of engine RPM and supercharger bypass valve
  position, not wastegate duty
- The "wastegate duty" table in the tune controls the bypass valve, not a
  traditional exhaust wastegate
- Knock sensitivity is different from a turbo engine — timing can often be
  more aggressive at low load, more conservative at peak boost

Stock peak output (CGWB): approximately 290 hp / 420 Nm at the crank.

---

## Calibration tables — what each one does

### Fueling

**MAF Transfer Function** (`maf_transfer`)  
Maps MAF sensor voltage (x-axis, mV×10) to calculated air mass in g/s.
This is the single most important table for a lean condition diagnosis.

- If the car is lean at ALL throttle positions and RPM points: this table is
  the first place to look. A wrong MAF transfer causes the ECU to
  underestimate actual airflow and add too little fuel.
- Common cause on 3.0T / 3.2T intake swaps: if a larger-bore intake
  manifold or different-diameter intake tract was installed, the stock MAF
  transfer no longer matches the airflow through the sensor.
- Safe edit: scale values proportionally. If the car is 5% lean across the
  board, increasing all values by approximately 5% is a starting point.
- Verify with STFT/LTFT live data — both banks should trim close to zero
  after correction.

**Injector Scaling** (`injector_scaling`)  
Base injector pulsewidth (ms) as a function of RPM (rows) and load in
mg/stroke (columns).

- If lean at ALL loads but MAF transfer looks correct: check injector flow
  rating. If higher-flow injectors were installed without recalibrating,
  the ECU is commanding correct pulsewidth but delivering too much or too
  little fuel.
- For stock injectors, this table should not need modification.
- 3.2T block swap: if the 3.2T intake manifold raises airflow substantially,
  the load axis values shift — the injector scaling may need adjustment even
  with stock injectors.

**Lambda Setpoint Maps** (`lambda_setpoint`, `lambda_setpoint_b2`)  
Target air-fuel ratio as lambda (1.000 = stoichiometric = 14.7:1 AFR).

- Under normal cruising: 1.000 (stoich)
- Under light throttle with catalyst heating: slightly lean (1.020–1.050)
- Under high load / WOT: rich enrichment, typically 0.860–0.900 for the 3.0T
- If target is wrong (e.g. showing 1.000 at WOT), the ECU is commanding lean
  at full load. This is dangerous and will cause knock and potentially piston
  damage. Check this table before any power modification.
- B1 and B2 should be identical on the CGWB. A significant difference
  between banks suggests a sensor fault rather than a calibration issue.

**Lambda Lean Limit** (`lambda_limit_lean`)  
Maximum lambda before the ECU stores a fault (P0171/P0174).

- Default approximately 1.150–1.200 depending on RPM
- Raising this mask faults without fixing the underlying condition — do not
  raise this table without fixing the root cause

### Ignition

**Ignition Advance Maps** (`ignition_advance`, `ignition_advance_b2`)  
Spark timing in degrees before top dead center (BTDC) as a function of RPM
and load.

- Positive values = advance (BTDC), negative = retard
- Stock CGWB: approximately 20–28° BTDC at light load, 8–14° at peak boost
- The ECU will pull timing if knock is detected — live knock retard channels
  show how much is being subtracted in real time
- Do not advance timing under boost without also monitoring knock retard live.
  The 3.0T is knock-sensitive on 91 octane; it is significantly more
  tolerant on 93 or E10.

**Knock Retard Limit** (`knock_retard_limit`)  
Maximum retard the knock control system can apply (degrees).

- Default approximately 8–10° depending on RPM
- If the ECU is hitting this limit regularly, it cannot protect the engine
  from knock. Reduce boost, advance timing less aggressively, or use
  higher-octane fuel.

### Boost

**Boost Setpoint** (`boost_setpoint`)  
Target boost pressure in bar absolute as a function of RPM (rows) and
throttle position % (columns).

- 1.000 bar = atmospheric (no boost)
- Stock CGWB: approximately 1.45–1.55 bar absolute at wide open throttle
  in mid-RPM range; falls off at high RPM as the supercharger bypass opens
- Increasing values here increases how hard the ECU commands the bypass valve
  to close, which increases boost
- Do not raise above approximately 1.80 bar without upgrading the
  intercooler, fueling, and timing tables

**Boost Limit** (`boost_limit`)  
Maximum boost allowed before a fault is stored.

- If the actual boost exceeds this, P0234 (overboost) stores
- Default approximately 1.75 bar absolute depending on RPM
- Should always be set above boost setpoint + margin

**Wastegate Duty Cycle** (`wastegate_duty`)  
On the 3.0T, this controls the supercharger bypass valve solenoid, not a
traditional turbo wastegate.

- Higher duty = bypass valve more closed = more boost
- Lower duty = bypass valve more open = less boost, reduced supercharger load
- This table is typically modified in conjunction with boost setpoint when
  tuning

### Throttle and torque

**Throttle Body Map** (`throttle_map`)  
Maps driver pedal position (%) to actual throttle blade angle (degrees).

- This is an electronic throttle control (drive-by-wire) linearization map
- Stock: slightly non-linear to give a progressive pedal feel
- Making this more linear can improve throttle response feel, particularly
  from a rolling speed

**Torque Limit Map** (`torque_limit`)  
Maximum torque per gear.

- Stock: approximately 400–440 Nm depending on gear and RPM
- The TCU (ZF 8HP) also has its own torque limit — both must be raised in
  concert for meaningful gains
- If the engine is making more power but the car doesn't feel faster in
  lower gears, the torque limiter is likely capping output

### Idle

**Idle Speed Target** (`idle_speed_target`)  
Target idle RPM as a function of coolant temperature.

- Cold start idle: approximately 1100–1400 RPM at -20°C
- Warm idle: 700–800 RPM
- Should not require modification for standard use

---

## Lean condition diagnosis flowchart

If the car stores P0171 (Bank 1 lean) or P0174 (Bank 2 lean):

```
1. Read LTFT B1 and LTFT B2 live
   │
   ├─ LTFT > +10% on BOTH banks → likely MAF transfer issue or air leak
   │   └─ Check MAF sensor, MAF transfer table, intake for unmetered air
   │
   ├─ LTFT > +10% on ONE bank only → bank-specific issue
   │   └─ Check O2 sensor, injectors, exhaust leak before cat
   │
   └─ LTFT near zero but P0171/P0174 stores at high load only
       └─ Check lambda setpoint at WOT — may be commanding lean target
          Check injector scaling at high load
          Check boost — high boost = more air, needs proportionally more fuel

2. If MAF is suspected:
   - Log MAF (g/s) at idle with AC off: expect 4–7 g/s on warm CGWB
   - Log MAF at 2000 rpm light throttle: expect 15–25 g/s
   - Significant deviation from these ranges → MAF sensor fault or
     MAF transfer table mismatch

3. If lean only under boost:
   - Check boost setpoint vs actual boost (MAP vs MAP SP in logger)
   - If boost is correct, check injector scaling at high-load columns
   - Check fuel pressure live — pressure drop under boost = pump or
     fuel delivery issue, not a calibration issue
```

The `diagnose_lean()` function in `tuner/cal_parser.py` runs a static
analysis of the loaded CAL binary and reports on the three most likely
sources of a lean condition based on the table values.

---

## Safe tuning limits — 3.0T TFSI CGWB, stock hardware

| Parameter | Stock | Safe max (93 oct) | Notes |
|-----------|-------|-------------------|-------|
| Boost (bar abs) | 1.50 | 1.75 | Intercooler limits above this |
| WOT lambda | 0.87 | 0.82 | Richer than 0.80 rarely beneficial |
| Peak timing (WOT) | 10–14° | 16° | Pull back if knock retard active |
| Torque limit | 440 Nm | 480 Nm | ZF 8HP rated ~600 Nm |
| Max RPM | 6,800 | 6,800 | Valve float risk above stock limit |

---

## What to request with the ES_LIBCompoProteGen3V12.sd.db file

In addition to the `.sd.db` file, the following from the ODIS-S
installation are useful:

**Priority 1 (needed for CP routine extraction):**
```
AU57X/ES_LIBCompoProteGen3V12.sd.db
AU57X/ES_LIBCompoProteGen3V12.bv.db   (if present alongside .sd.db)
```

**Priority 2 (confirm routine ID and token structure):**
```
AU57X/BV_GatewUDS.sd.db
AU57X/BV_GatewUDS.bv.db
AU57X/BV_AirCondiUDS.sd.db
```

**Priority 3 (verify J255 IKA/GKA key write structure):**
```
AU57X/BV_AirCondiUDS.bv.db
EV_GatewPkoUDS_001_AU57.odx           (from diagdata folder, not PostSetup)
EV_AirCondiBasisUDS_002_AU57.odx
```

**How to find them:**
```
ODIS-S PostSetup path:
  C:\ProgramData\ODIS-S\PostSetup\VW\[brand]\...
  C:\ProgramData\ODIS-S\diagdata\EV_GatewPkoUDS\*.odx

MCD project path (what we need):
  C:\ProgramData\ODIS-S\mcd\AU57X\*.sd.db
  C:\ProgramData\ODIS-S\mcd\AU57X\*.bv.db
```

The `.sd.db` files are small (10–50 KB each). The full AU57X folder is
approximately 200–400 MB. If zipping the full folder is impractical,
just the `.sd.db` and `.bv.db` files for the libraries listed above
are sufficient.

---

*See also: VAG-CP-Docs technical/ for Component Protection research*
