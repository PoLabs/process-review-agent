---
doc_id: SAF-INC-001
title: Chemical incompatibility matrix
store: safety
version: D
status: approved
effective: 2025-06-01
amends: 
covers: acetone, hydrogen_peroxide, nitric_acid, ethanol, isopropanol, sodium_hypochlorite, ammonia, hydrochloric_acid
---

## Purpose

This matrix lists chemical pairs that must not be present together in any vessel, line or sink. A pair listed here is a stop: the process is not run until the plan is changed.

## Solvents and oxidizers

Acetone must never be combined with hydrogen peroxide. Acetone must never be combined with nitric acid. Ethanol must never be combined with nitric acid. Isopropanol must never be combined with nitric acid.

## Hypochlorite

Sodium hypochlorite must never be combined with ammonia. Sodium hypochlorite must never be combined with hydrochloric acid.

## Review

The matrix is reviewed yearly by the EHS lead. Any pair not listed is not thereby safe: check the safety data sheets of both materials.

```rules
[
  {
    "kind": "incompatible",
    "severity": "critical",
    "a": "acetone",
    "b": "hydrogen_peroxide",
    "hazard": "forms explosive organic peroxides",
    "quote": "Acetone must never be combined with hydrogen peroxide."
  },
  {
    "kind": "incompatible",
    "severity": "critical",
    "a": "acetone",
    "b": "nitric_acid",
    "hazard": "violent exothermic reaction",
    "quote": "Acetone must never be combined with nitric acid."
  },
  {
    "kind": "incompatible",
    "severity": "critical",
    "a": "ethanol",
    "b": "nitric_acid",
    "hazard": "violent exothermic reaction",
    "quote": "Ethanol must never be combined with nitric acid."
  },
  {
    "kind": "incompatible",
    "severity": "critical",
    "a": "isopropanol",
    "b": "nitric_acid",
    "hazard": "violent exothermic reaction",
    "quote": "Isopropanol must never be combined with nitric acid."
  },
  {
    "kind": "incompatible",
    "severity": "critical",
    "a": "sodium_hypochlorite",
    "b": "ammonia",
    "hazard": "releases toxic chloramine gas",
    "quote": "Sodium hypochlorite must never be combined with ammonia."
  },
  {
    "kind": "incompatible",
    "severity": "critical",
    "a": "sodium_hypochlorite",
    "b": "hydrochloric_acid",
    "hazard": "releases toxic chlorine gas",
    "quote": "Sodium hypochlorite must never be combined with hydrochloric acid."
  }
]
```
