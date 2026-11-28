# aWATTar Pricing Module

## Introduction

The aWATTar pricing module reads the current wholesale spot-market import price
from the aWATTar market-data API (Austria / Germany). Prices are returned in
EUR per kWh (converted from the API's EUR/MWh).

aWATTar asks that the API be queried no more than once every 15 minutes; the
module caches accordingly.

The price is available to charging policy as
`modules.aWATTarPricing.importPrice`. aWATTar has no export price.

### Forecasting

aWATTar gives the prices for the next 24 hours, so this module can find the
cheapest hours to charge in; see [Pricing Modules](Pricing.md). The forecast
comes with the current price, in the same request.

### Status

| Detail          | Value                          |
| --------------- | ------------------------------ |
| **Module Name** | aWATTar                        |
| **Module Type** | Pricing                        |
| **Features**    | Import price, Forecasting      |
| **Status**      | Implemented, Tested            |

## Configuration

| Parameter | Value |
| --------- | ----- |
| enabled   | *required* Boolean, `true` or `false`. |
| cheapWindow | *optional* Cheapest-window charging; see [Pricing Modules](Pricing.md#charging-in-the-cheapest-window). |

### JSON Configuration Example

```
"pricing": {
  "aWATTar": {
    "enabled": true,
    "cheapWindow": { "hours": 3, "startHour": 22, "endHour": 5 }
  }
}
```
