---
doc_id: SOP-SAF-012
title: Process temperature limits for flammable solvents
store: safety
version: C
status: approved
effective: 2026-03-01
amends: 
covers: acetone, toluene, ethanol, isopropanol
---

## Scope

This procedure sets the highest temperature at which a flammable solvent may be held or processed. It applies to every vessel on the floor, and a process above a limit is not started.

## Acetone

The maximum process temperature for acetone is 35 °C. Measure the temperature at the vessel, not at the jacket.

## Toluene

The maximum process temperature for toluene is 60 °C. Keep the vessel closed and vented to the extraction line.

## Ethanol

The maximum process temperature for ethanol is 40 °C. Keep the vessel closed while heated.

## Isopropanol

The maximum process temperature for isopropanol is 40 °C. Keep the vessel closed while heated.

```rules
[
  {
    "kind": "chem_limit",
    "severity": "major",
    "chemical": "acetone",
    "max_temp_c": 35,
    "hazard": "flammable solvent vapour above limit",
    "quote": "The maximum process temperature for acetone is 35 °C."
  },
  {
    "kind": "chem_limit",
    "severity": "major",
    "chemical": "toluene",
    "max_temp_c": 60,
    "hazard": "flammable solvent vapour above limit",
    "quote": "The maximum process temperature for toluene is 60 °C."
  },
  {
    "kind": "chem_limit",
    "severity": "major",
    "chemical": "ethanol",
    "max_temp_c": 40,
    "hazard": "flammable solvent vapour above limit",
    "quote": "The maximum process temperature for ethanol is 40 °C."
  },
  {
    "kind": "chem_limit",
    "severity": "major",
    "chemical": "isopropanol",
    "max_temp_c": 40,
    "hazard": "flammable solvent vapour above limit",
    "quote": "The maximum process temperature for isopropanol is 40 °C."
  }
]
```
