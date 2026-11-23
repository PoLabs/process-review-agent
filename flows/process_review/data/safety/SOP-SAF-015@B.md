---
doc_id: SOP-SAF-015
title: Oxidizer handling
store: safety
version: B
status: draft
effective: 2027-01-01
amends: 
covers: hydrogen_peroxide, sodium_hypochlorite
---

## Scope

This procedure covers oxidizing liquids used on the floor. This revision is a draft awaiting EHS approval and is not in force.

## Hydrogen peroxide

Hydrogen peroxide may be used at a maximum concentration of 50 % and a maximum process temperature of 40 °C. Use only clean, dedicated vessels and keep it from all organic material.

## Sodium hypochlorite

Sodium hypochlorite may be used at a maximum concentration of 12.5 %. Dilute only into clean water and never into a vessel that held acid or ammonia.

```rules
[
  {
    "kind": "chem_limit",
    "severity": "major",
    "chemical": "hydrogen_peroxide",
    "max_temp_c": 40,
    "max_concentration_pct": 50,
    "hazard": "decomposes and releases oxygen above limit",
    "quote": "Hydrogen peroxide may be used at a maximum concentration of 50 % and a maximum process temperature of 40 °C."
  },
  {
    "kind": "chem_limit",
    "severity": "major",
    "chemical": "sodium_hypochlorite",
    "max_concentration_pct": 12.5,
    "hazard": "decomposes and releases chlorine above limit",
    "quote": "Sodium hypochlorite may be used at a maximum concentration of 12.5 %."
  }
]
```
