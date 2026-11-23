---
doc_id: MM-M7
title: Manual: open-top mixing tank M-7
store: maintenance
version: 2
status: approved
effective: 2025-02-01
amends: 
covers: M-7
---

## Description

Manual: open-top mixing tank M-7. This manual is kept by the maintenance team for the operators of M-7.

## Ratings

M-7 is an open-top tank for atmospheric use only (0 bar gauge), with a maximum temperature of 60 °C. It must not be closed or pressurized.

## Status

M-7 is in service.

## Maintenance

Inspect gaskets and seals every quarter and record the result in the equipment log. Report any leak or abnormal noise to maintenance before the next use.

```rules
[
  {
    "kind": "equipment",
    "severity": "critical",
    "equipment": "M-7",
    "max_temp_c": 60,
    "max_pressure_bar": 0,
    "in_service": true,
    "hazard": "open vessel releases vapour",
    "quote": "M-7 is an open-top tank for atmospheric use only (0 bar gauge), with a maximum temperature of 60 °C."
  }
]
```
