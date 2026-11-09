# aWATTar Pricing Module

## Introduction

The aWATTar pricing module reads the current wholesale spot-market import price
from the aWATTar market-data API (Austria / Germany). Prices are returned in
EUR per kWh (converted from the API's EUR/MWh).

aWATTar asks that the API be queried no more than once every 15 minutes; the
module caches accordingly.

The price is available to charging policy as
`modules.aWATTarPricing.importPrice`; see [Pricing Modules](Pricing.md).

### Status

| Detail          | Value                          |
| --------------- | ------------------------------ |
| **Module Name** | aWATTar                        |
| **Module Type** | Pricing                        |
| **Features**    | Import price                   |
| **Status**      | Implemented, Tested            |

## Configuration

| Parameter | Value |
| --------- | ----- |
| enabled   | *required* Boolean, `true` or `false`. |

### JSON Configuration Example

```
"pricing": {
  "aWATTar": {
    "enabled": true
  }
}
```
