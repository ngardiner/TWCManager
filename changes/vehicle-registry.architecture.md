---
pr: 703
authors: MikeBishop
---

Keep every per-vehicle field in one registry keyed by VIN, forgetting cars unseen for 30 days (`vehicle.forgetAfterDays`). The debug API `/api/sendTeslaAPICommand` takes `vehicleVIN` in place of `vehicleID`.
