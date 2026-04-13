# PVPCes Pricing Module

## Introduction

The PVPCes (Precio Voluntario para el Pequeño Consumidor) module fetches regulated electricity prices for Spain's small consumer tariff from the REE (Red Eléctrica de España) API at `api.esios.ree.es`. Prices are published daily at 20:30 CET for the following day and vary significantly by hour (typically 300–400% spread between cheapest and most expensive hours).

An API token is required; register at [https://www.esios.ree.es/en/](https://www.esios.ree.es/en/).

### Status

| Detail          | Value                                   |
| --------------- | --------------------------------------- |
| **Module Name** | PVPCesPricing                           |
| **Module Type** | Pricing                                 |
| **Features**    | Import price, cheapest-window selection |
| **Status**      | Implemented, Stable                     |

## Configuration

```json
"pricing": {
    "PVPCes": {
        "enabled": true,
        "token": "your-esios-api-token-here"
    }
}
```

### Parameters

| Parameter | Value |
| --------- | ----- |
| `enabled` | *required* `true` or `false` |
| `token`   | *required* API token from esios.ree.es |

## Cheapest-Window Scheduling (Flex Cheaper)

The PVPCes module exposes a `getCheapestStartHour(numHours, ini, end)` method that finds the cheapest contiguous block of hours within a configured window. This is designed for overnight charging where you need, for example, 5 hours of charging within a 22:00–07:00 window and want to pick the cheapest consecutive slot.

| Parameter  | Description |
| ---------- | ----------- |
| `numHours` | Number of consecutive hours needed |
| `ini`      | Start of the allowed window (hour 0–23) |
| `end`      | End of the allowed window (hour 0–23); if less than `ini`, the window spans midnight |

## Cache Behaviour

Prices are fetched once per day. The cache expires at midnight (detected by comparing the current hour with the hour of the last fetch). A failed network request does not update the cache timestamp, allowing a retry on the next poll cycle.

## Policy Integration

See [Pricing_Static.md](Pricing_Static.md) for policy rule examples using `getImportPrice()` and `getExportPrice()`.

## Dashboard

When any Pricing module is active, the current import and export prices are displayed on the main dashboard and refreshed every 30 seconds.
