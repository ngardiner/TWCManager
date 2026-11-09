# Tesla Pricing Module

## Introduction

The Tesla pricing module reports electricity prices from the rate plan set for
your Powerwall in the Tesla app (its utility rate plan). Both time-of-use and
flat-rate plans work.

Prices are available to charging policy as `modules.TeslaPricing.importPrice`
and `modules.TeslaPricing.exportPrice`, in the plan's currency per kWh. The
export price comes from the plan's sell rates; if the plan has none, there is
no export price.

### Requirements

- The [Powerwall](EMS_Powerwall2.md) source, configured and enabled.
- Tesla API access that can read energy data: a Fleet API token with the
  `energy_device_data` scope, or an Owner API token. This is the same access
  the Powerwall's `touPeriod` needs, and reading the plan is free.

The plan is re-read every `cloudUpdateInterval` (30 minutes by default), so a
change in the Tesla app shows up within that time. With only an Owner API
token, times follow the clock of the computer running TWCManager rather than
the Powerwall site's time zone.

### Forecasting

A rate plan is a fixed schedule, so this module knows the next 48 hours of
prices and can find the cheapest hours to charge in; see
[Pricing Modules](Pricing.md).

### Status

| Detail          | Value                                    |
| --------------- | ---------------------------------------- |
| **Module Name** | Tesla                                    |
| **Module Type** | Pricing                                  |
| **Features**    | Import price, Export price, Forecasting  |
| **Status**      | Implemented, Tested                      |

## Configuration

| Parameter | Value |
| --------- | ----- |
| enabled   | *required* Boolean, `true` or `false`. |
| cheapWindow | *optional* Cheapest-window charging; see [Pricing Modules](Pricing.md#charging-in-the-cheapest-window). |

### JSON Configuration Example

```
"pricing": {
  "Tesla": {
    "enabled": true,
    "cheapWindow": {
      "hours": 4,
      "startHour": 22,
      "endHour": 7
    }
  }
}
```

## FAQ

### The price shows as blank

Check that the Powerwall source is enabled and that the Tesla API is connected
with access to energy data. The plan is read on the first price check after
startup; if the Tesla app has no rate plan for the site, there is nothing to
report.
