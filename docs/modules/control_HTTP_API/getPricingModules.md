# getPricingModules API Command

## Introduction

The getPricingModules API command returns a list of all configured pricing modules with their status, capabilities, and current prices.

## Format of Request

```
GET /api/getPricingModules
```

## Format of Response

```json
[
    {
        "name": "Static",
        "enabled": true,
        "capabilities": {"AdvancePricing": true},
        "importPrice": 0.25,
        "exportPrice": 0.08
    },
    {
        "name": "Amber",
        "enabled": true,
        "capabilities": {
            "AdvancePricing": true,
            "SpikeDetection": true,
            "Renewables": true,
            "Forecasting": true
        },
        "importPrice": 0.35,
        "exportPrice": 0.12
    }
]
```

### Response Fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Module name (e.g., "Static", "Amber", "aWATTar") |
| `enabled` | boolean | Whether the module is currently active |
| `capabilities` | object | Dictionary of supported capabilities |
| `importPrice` | float | Current import price from this module |
| `exportPrice` | float | Current export price from this module |

### Capability Flags

| Capability | Description |
|------------|-------------|
| `AdvancePricing` | Supports advanced pricing features |
| `SpikeDetection` | Can detect price spikes |
| `Renewables` | Provides renewable percentage data |
| `Forecasting` | Supports price forecasting |

## Example

```bash
curl http://192.168.1.1:8080/api/getPricingModules
```

## Notes

- Returns an empty array if no pricing modules are configured
- Disabled modules are included in the response with `enabled: false`
