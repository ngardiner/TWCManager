# aWATTar Pricing Module

## Introduction

The aWATTar Pricing module fetches real-time day-ahead electricity spot market prices from the [aWATTar API](https://www.awattar.at/) for Austria and Germany. Prices reflect the hourly EPEX spot market and can be used to stop charging during expensive periods.

aWATTar requests that clients limit API queries to once every 15 minutes. This module enforces that limit automatically.

### Status

| Detail          | Value                     |
| --------------- | ------------------------- |
| **Module Name** | aWATTarPricing            |
| **Module Type** | Pricing                   |
| **Features**    | Import price (spot market) |
| **Status**      | Implemented, Stable       |

### Note

aWATTar provides import (buy) prices only. Export prices will always return 0 from this module. If you need export pricing, combine with the Static Pricing module using `multiPrice: "add"`.

## Configuration

```json
"pricing": {
    "aWATTar": {
        "enabled": true
    }
}
```

### Parameters

| Parameter | Value |
| --------- | ----- |
| `enabled` | *required* `true` or `false` |

### Multiple Pricing Modules

If you run both aWATTar and Static Pricing simultaneously, configure the `multiPrice` policy in the `pricing.policy` section:

```json
"pricing": {
    "policy": {
        "multiPrice": "first"
    },
    "aWATTar": {
        "enabled": true
    }
}
```

| Policy  | Behaviour |
| ------- | --------- |
| `first` | Use the first non-zero price found (default) |
| `add`   | Sum prices from all active modules |

## Policy Integration

See [Pricing_Static.md](Pricing_Static.md) for policy rule examples using `getImportPrice()` and `getExportPrice()`.

## Dashboard

When any Pricing module is active, the current import and export prices are displayed on the main dashboard and refreshed every 30 seconds.

## API Endpoints

The following API endpoints are available for pricing integration:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/getPricing` | GET | Returns current import and export prices |
| `/api/getPricingModules` | GET | Lists all configured pricing modules with status |
| `/api/getPricingDetails` | GET | Detailed info for specific module (`?module=aWATTar`) |
| `/api/refreshPricing` | POST | Force refresh pricing data from all modules |

### Example API Responses

**GET /api/getPricing**
```json
{
    "import": 0.18,
    "export": 0.00
}
```

**GET /api/getPricingModules**
```json
[
    {
        "name": "aWATTar",
        "enabled": true,
        "capabilities": {"AdvancePricing": true},
        "importPrice": 0.18,
        "exportPrice": 0.00
    }
]
```

## UI

When pricing modules are configured, a **Pricing** link appears in the navigation bar between Policy and Schedule. The pricing page displays:

- Active pricing modules with their current prices
- Price levels (color-coded: green=low, yellow=medium, red=high)
- Module capabilities and status

Note: aWATTar does not support price forecasting. The forecast table and optimal window finder require a module with forecasting capability (e.g., Amber).
