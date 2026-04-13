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
