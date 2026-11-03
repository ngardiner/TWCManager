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

### Forecasting

Amber's API returns a forward price series, so this module also offers the
`Forecasting` capability. When a `cheapWindow` is configured, the module scans
the forecast for the cheapest contiguous block of the requested number of hours
and exposes:

- `cheapWindowActive`: `1` while the current time falls inside the cheapest
  window, otherwise `0`. Use this in policy to charge only during the cheapest
  part of the day.
- `forecastMinPrice` / `forecastMaxPrice`: the lowest and highest import price
  across the forecast horizon.
- `cheapWindowStart`: ISO 8601 start time of the chosen window (for display).

The forecast is also available over the HTTP API at `/api/getPricingForecast`
(see `docs/modules/control_HTTP_API/getPricingForecast.md`). If no `cheapWindow`
is configured, the forecast is still fetched for the API/UI but no window is
computed and `cheapWindowActive` stays `0`, leaving charging behaviour
unchanged.

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
| cheapWindow | *optional* Object enabling cheapest-window forecasting. |
| cheapWindow.hours | Number of consecutive hours the window should span (e.g. `5`). |
| cheapWindow.startHour | *optional* Earliest local hour (0-23) the window may start. |
| cheapWindow.endHour | *optional* Latest local hour (0-23) the window may run to; if less than `startHour`, the range crosses midnight. |

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
