# Amber Electric Pricing Module

## Introduction

The Amber pricing module reads the current import (general) and export (feedIn)
price for your Amber Electric site, in AUD per kWh. Amber passes through
Australian wholesale pricing, so prices vary through the day and can be
negative.

Obtain an API token from the Amber app (Settings > Developers). The site ID is
discovered automatically from the token; you may also set it explicitly.

Prices are exposed to charging policy as `modules.AmberPricing.importPrice` and
`modules.AmberPricing.exportPrice` (see `docs/PolicyCustomization.md`).

> **Note:** This Phase 1 module reports the *current* price only. Price
> forecasting and cheapest-window scheduling are planned for a later release, so
> the module does not advertise the `Forecasting` capability.

### Status

| Detail          | Value                                   |
| --------------- | --------------------------------------- |
| **Module Name** | Amber                                   |
| **Module Type** | Pricing                                 |
| **Features**    | Import price, Export price, Spike/Renewables |
| **Status**      | Implemented, Tested                     |

## Configuration

| Parameter | Value |
| --------- | ----- |
| enabled   | *required* Boolean, `true` or `false`. |
| token     | *required* Amber API token. |
| siteId    | *optional* Amber site ID. Auto-discovered from the token if omitted. |
| cacheTime | *optional* Minimum seconds between API fetches (default 300). |

### JSON Configuration Example

```
"pricing": {
  "Amber": {
    "enabled": true,
    "token": "your-amber-api-token"
  }
}
```
