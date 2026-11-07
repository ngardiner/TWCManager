# Tesla Pricing Module

## Introduction

The Tesla pricing module reports electricity prices from the rate plan set for
your Powerwall in the Tesla app (its utility rate plan). Both time-of-use and
flat-rate plans work.

Prices are exposed to charging policy as `modules.TeslaPricing.importPrice` and
`modules.TeslaPricing.exportPrice` (see `docs/PolicyCustomization.md`), in the
plan's currency per kWh. The export price comes from the plan's sell rates;
if the plan has none, there is no export price.

When a price is not known (the plan has not been read yet, or gives no rate
for the current time), the price is left out rather than shown as 0, so a
policy comparing it does not match.

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

A rate plan is a fixed schedule, so this module knows the price for the next
48 hours without asking anyone, and offers the `Forecasting` capability. When a
`cheapWindow` is configured, it finds the cheapest block of the requested
number of hours and exposes:

- `cheapWindowActive`: `1` while the current time falls inside the cheapest
  window, otherwise `0`. Use this in policy to charge only during the cheapest
  part of the day.
- `forecastMinPrice` / `forecastMaxPrice`: the lowest and highest import price
  over the next 48 hours.
- `cheapWindowStart`: start time of the chosen window (for display).

On a time-of-use plan several blocks are often equally cheap; the earliest one
is chosen. The forecast is also available over the HTTP API at
`/api/getPricingForecast` (see `docs/modules/control_HTTP_API/getPricingForecast.md`).

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
| cheapWindow | *optional* Object enabling cheapest-window charging. |
| cheapWindow.hours | Number of consecutive hours the window should span (e.g. `4`). |
| cheapWindow.startHour | *optional* Earliest local hour (0-23) the window may start. |
| cheapWindow.endHour | *optional* Latest local hour (0-23) the window may run to; if less than `startHour`, the range crosses midnight. |

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
