---
doc_id: SAF-INC-001
title: Chemical incompatibility matrix
store: safety
version: C
status: superseded
effective: 2024-01-01
amends: 
covers: acetone, hydrogen_peroxide, sodium_hypochlorite, ammonia
---

## Purpose

This matrix lists chemical pairs that must not be present together in any vessel, line or sink.

## Listed pairs

Acetone must never be combined with hydrogen peroxide. Sodium hypochlorite must never be combined with ammonia.

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
    "a": "sodium_hypochlorite",
    "b": "ammonia",
    "hazard": "releases toxic chloramine gas",
    "quote": "Sodium hypochlorite must never be combined with ammonia."
  }
]
```
