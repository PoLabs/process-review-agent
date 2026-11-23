---
doc_id: MM-SC1
title: Manual: fume scrubber SC-1
store: maintenance
version: 2
status: approved
effective: 2026-10-20
amends: 
covers: SC-1
---

## Description

Manual: fume scrubber SC-1. This manual is kept by the maintenance team for the operators of SC-1.

## Status

SC-1 is out of service: it is locked out until 2026-12-15 for packing replacement. No process that depends on the scrubber is started while the lockout tag is on.

## Ratings

The scrubber handles acid fume from the blending bay. It is returned to service only after maintenance signs the lockout card.

## Maintenance

Inspect gaskets and seals every quarter and record the result in the equipment log. Report any leak or abnormal noise to maintenance before the next use.

```rules
[
  {
    "kind": "equipment",
    "severity": "critical",
    "equipment": "SC-1",
    "in_service": false,
    "hazard": "no fume extraction",
    "quote": "SC-1 is out of service: it is locked out until 2026-12-15 for packing replacement."
  }
]
```
