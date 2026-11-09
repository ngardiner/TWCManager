# Pricing Modules

## Introduction

A Pricing module tells TWCManager what electricity costs right now, so a
charging policy can charge when power is cheap and stop when it is dear. Some
modules also know prices ahead of time, and can find the cheapest few hours to
charge in.

| Module                          | Prices from                              | Export price | Forecast |
| ------------------------------- | ---------------------------------------- | ------------ | -------- |
| [Amber](Pricing_Amber.md)       | Amber Electric (Australia)               | Yes          | Yes      |
| [aWATTar](Pricing_aWATTar.md)   | aWATTar spot market (Austria / Germany)  | No           | No       |
| [Static](Pricing_Static.md)     | Fixed prices you set                     | Yes          | No       |
| [Tesla](Pricing_Tesla.md)       | The rate plan set in the Tesla app       | Yes          | Yes      |

Each module's page covers its own setup. This page covers what they have in
common.

## Using prices in policy

A Pricing module's values are available to policy as
`modules.<Module>.<value>`, for example `modules.AmberPricing.importPrice`. See
[Price-based charging](../PolicyCustomization.md#price-based-charging) for
example policies.

| Value               | Meaning |
| ------------------- | ------- |
| `importPrice`       | The current price to buy electricity, in currency per kWh. Wholesale prices can be negative. |
| `exportPrice`       | The current price paid for electricity sent to the grid, in currency per kWh. |
| `cheapWindowActive` | Forecasting modules: `1` while the current time is inside the cheapest window, otherwise `0`. |
| `cheapWindowStart`  | Forecasting modules: when the cheapest window starts (ISO 8601), for display. |
| `forecastMinPrice` / `forecastMaxPrice` | Forecasting modules: the lowest and highest import price over the next 48 hours. |

Some modules offer more; see their pages.

## Charging in the cheapest window

A module with a forecast can look over the next 48 hours of prices and find the
cheapest unbroken run of the number of hours you need. Add a `cheapWindow` to
the module's configuration:

| Parameter             | Value |
| --------------------- | ----- |
| cheapWindow.hours     | Number of consecutive hours the window should span, from 1 to 24 (e.g. `4`). |
| cheapWindow.startHour | *optional* Earliest local hour (0-23) the window may start. |
| cheapWindow.endHour   | *optional* Local hour (0-23) the window must finish by; if less than `startHour`, the range crosses midnight. Must differ from `startHour`, and is used only with it. |

```
"pricing": {
  "Tesla": {
    "enabled": true,
    "cheapWindow": { "hours": 4, "startHour": 22, "endHour": 7 }
  }
}
```

Then charge while `modules.<Module>.cheapWindowActive` is `1`; see
[Charging in the cheapest forecast window](../PolicyCustomization.md#charging-in-the-cheapest-forecast-window).

When several windows are equally cheap, the earliest is chosen. Once a window
starts, it runs for its full length even if a later forecast finds a cheaper
one. Without a `cheapWindow`, `cheapWindowActive` stays `0` and charging is
unaffected.

## When a price is not known yet

Until a module has a price, for example just after startup or while its
provider cannot be reached:

- **Tesla** leaves the price out. A policy that compares it with `lt`, `lte`,
  `gt` or `gte`, such as `importPrice lt 0.10`, does not match, so charging is
  unaffected. With `ne`, a missing price counts as not equal and does match.
- **Amber** and **aWATTar** report `0` until their first price arrives.
  aWATTar has no export price and always reports `0` for it.
- **Static** always knows its prices.

With a forecast, `forecastMinPrice` and `forecastMaxPrice` are left out until
the first forecast arrives, and `cheapWindowActive` is `0`.

The same holds for a value from a module that is not loaded.

## HTTP API

- [getPricing](control_HTTP_API/getPricing.md): each module's current import
  and export price.
- [getPricingForecast](control_HTTP_API/getPricingForecast.md): each
  forecasting module's price forecast and cheapest window.
