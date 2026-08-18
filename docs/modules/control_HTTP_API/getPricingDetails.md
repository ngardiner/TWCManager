# getPricingDetails API Command

## Introduction

The getPricingDetails API command returns detailed information about pricing modules, including extended data like spike status, renewables percentage, and price descriptors.

## Format of Request

Get details for all modules:
```
GET /api/getPricingDetails
```

Get details for a specific module:
```
GET /api/getPricingDetails?module=Amber
```

### Query Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `module` | no | Module name to get details for. If omitted, returns details for all modules. |

## Format of Response

**All modules:**
```json
[
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
        "exportPrice": 0.12,
        "spikeStatus": "none",
        "renewables": 55,
        "priceDescriptor": "neutral",
        "hasForecast": true
    }
]
```

**Specific module:**
```json
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
    "exportPrice": 0.12,
    "spikeStatus": "none",
    "renewables": 55,
    "priceDescriptor": "neutral",
    "hasForecast": true
}
```

**Module not found:**
```json
{
    "error": "Module not found"
}
```

### Response Fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Module name |
| `enabled` | boolean | Whether the module is active |
| `capabilities` | object | Supported capabilities |
| `importPrice` | float | Current import price |
| `exportPrice` | float | Current export price |
| `spikeStatus` | string | Price spike status: `none`, `potential`, or `spike` (if supported) |
| `renewables` | integer | Grid renewable percentage 0-100 (if supported) |
| `priceDescriptor` | string | Human-readable price level (if supported) |
| `hasForecast` | boolean | Whether forecasting is available (if supported) |

### Price Descriptors

| Descriptor | Meaning |
|------------|---------|
| `extremelyLow` | Very cheap (often negative prices) |
| `veryLow` | Below average |
| `low` | Slightly below average |
| `neutral` | Around average |
| `high` | Above average |
| `spike` | Significantly above average |

## Example

```bash
# Get all module details
curl http://192.168.1.1:8080/api/getPricingDetails

# Get Amber module details
curl "http://192.168.1.1:8080/api/getPricingDetails?module=Amber"
```

## Notes

- Extended fields (spikeStatus, renewables, priceDescriptor) are only included for modules that support those capabilities
- If a module doesn't support a capability, the corresponding field is omitted
