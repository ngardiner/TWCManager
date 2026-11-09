# Amber Electric Pricing Module

## Introduction

The Amber pricing module reads the current import (general) and export (feedIn)
price for your Amber Electric site, in AUD per kWh. Amber passes through
Australian wholesale pricing, so prices vary through the day and can be
negative.

Obtain an API token from the Amber app (Settings > Developers). The site ID is
discovered automatically from the token; you may also set it explicitly.

Prices are available to charging policy as `modules.AmberPricing.importPrice`
and `modules.AmberPricing.exportPrice`, along with `spikeStatus` (Amber's
wholesale spike state) and `renewables` (the grid's current renewables
percentage).

Amber publishes prices ahead of time, so this module can also find the
cheapest hours to charge in; see [Pricing Modules](Pricing.md) for this, and
for what happens before the first price arrives. The forecast is refreshed
every 15 minutes.

### Status

| Detail          | Value                                   |
| --------------- | --------------------------------------- |
| **Module Name** | Amber                                   |
| **Module Type** | Pricing                                 |
| **Features**    | Import price, Export price, Forecasting, Spike/Renewables |
| **Status**      | Implemented, Tested                     |

## Configuration

| Parameter | Value |
| --------- | ----- |
| enabled   | *required* Boolean, `true` or `false`. |
| token     | *required* Amber API token. |
| siteId    | *optional* Amber site ID. Auto-discovered from the token if omitted. |
| cacheTime | *optional* Minimum seconds between current-price API fetches (default 300). |
| cheapWindow | *optional* Cheapest-window charging; see [Pricing Modules](Pricing.md#charging-in-the-cheapest-window). |

### JSON Configuration Example

```
"pricing": {
  "Amber": {
    "enabled": true,
    "token": "your-amber-api-token",
    "cheapWindow": {
      "hours": 5,
      "startHour": 22,
      "endHour": 7
    }
  }
}
```
