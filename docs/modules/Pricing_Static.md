# Static Pricing Module

## Introduction

The Static Pricing module provides fixed import and export electricity prices to TWCManager for environments where dynamic pricing APIs are not available or not needed. Prices are configured directly in `config.json`.

### Status

| Detail          | Value                     |
| --------------- | ------------------------- |
| **Module Name** | StaticPricing             |
| **Module Type** | Pricing                   |
| **Features**    | Import price, Export price |
| **Status**      | Implemented, Stable       |

## Configuration

Add a `pricing` section to your `config.json`:

```json
"pricing": {
    "Static": {
        "enabled": true,
        "peak": {
            "import": 0.25,
            "export": 0.10
        }
    }
}
```

### Parameters

| Parameter         | Value |
| ----------------- | ----- |
| `enabled`         | *required* `true` or `false` |
| `peak.import`     | *required* Import price per kWh (numeric) |
| `peak.export`     | *required* Export price per kWh (numeric) |

## Policy Integration

Once configured, pricing data is available to the policy engine via the `getImportPrice()` and `getExportPrice()` functions. Example policy rules are provided in the `policy.extend.emergency` section of `config.json`.

### Stop charging when import price exceeds a maximum

```json
"config": {
    "maxImportPrice": 0.30
},
"policy": {
    "extend": {
        "emergency": [
            { "name": "Import Price Too High",
              "match": [ "getImportPrice()" ],
              "condition": [ "gt" ],
              "value": [ "config.maxImportPrice" ],
              "charge_amps": 0 }
        ]
    }
}
```

### Stop charging when export price exceeds import price

```json
"policy": {
    "extend": {
        "emergency": [
            { "name": "Export Price Favourable",
              "match": [ "getExportPrice()" ],
              "condition": [ "gt" ],
              "value": [ "getImportPrice()" ],
              "charge_amps": 0 }
        ]
    }
}
```

## Dashboard

When any Pricing module is active, the current import and export prices are displayed on the main dashboard and refreshed every 30 seconds.

## API Endpoints

The following API endpoints are available for pricing integration:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/getPricing` | GET | Returns current import and export prices |
| `/api/getPricingModules` | GET | Lists all configured pricing modules with status |
| `/api/getPricingDetails` | GET | Detailed info for specific module (`?module=Static`) |
| `/api/refreshPricing` | POST | Force refresh pricing data from all modules |

### Example API Responses

**GET /api/getPricing**
```json
{
    "import": 0.25,
    "export": 0.10
}
```

**GET /api/getPricingModules**
```json
[
    {
        "name": "Static",
        "enabled": true,
        "capabilities": {"AdvancePricing": true},
        "importPrice": 0.25,
        "exportPrice": 0.10
    }
]
```

## UI

When pricing modules are configured, a **Pricing** link appears in the navigation bar between Policy and Schedule. The pricing page displays:

- Active pricing modules with their current prices
- Price levels (color-coded: green=low, yellow=medium, red=high)
- Price forecast table (for modules with forecasting capability)
- Optimal charging window finder tool
