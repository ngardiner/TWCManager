# getPricingForecast API Command

## Introduction

The getPricingForecast API command returns price forecast data for future time intervals. Requires a pricing module with forecasting capability (e.g., Amber).

## Format of Request

```
GET /api/getPricingForecast?hours=24
```

### Query Parameters

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `hours` | no | 24 | Number of hours to forecast (1-48) |

## Format of Response

```json
{
    "hoursRequested": 24,
    "forecast": [
        {
            "timestamp": "2025-01-15T14:00:00",
            "importPrice": 0.32,
            "exportPrice": 0.10,
            "spikeStatus": "none",
            "descriptor": "neutral",
            "renewables": 45
        },
        {
            "timestamp": "2025-01-15T14:30:00",
            "importPrice": 0.28,
            "exportPrice": 0.08,
            "spikeStatus": "none",
            "descriptor": "low",
            "renewables": 52
        }
    ]
}
```

### Response Fields

| Field | Type | Description |
|-------|------|-------------|
| `hoursRequested` | integer | Number of hours requested |
| `forecast` | array | Array of forecast entries |

### Forecast Entry Fields

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | string | ISO 8601 timestamp for interval start |
| `importPrice` | float | Predicted import price in $/kWh |
| `exportPrice` | float | Predicted export price in $/kWh |
| `spikeStatus` | string | `none`, `potential`, or `spike` |
| `descriptor` | string | Human-readable price level |
| `renewables` | integer | Predicted renewable percentage |

## Example

```bash
# Get 12-hour forecast
curl "http://192.168.1.1:8080/api/getPricingForecast?hours=12"

# Get 24-hour forecast (default)
curl http://192.168.1.1:8080/api/getPricingForecast
```

## Notes

- Returns an empty forecast array if no forecasting-capable modules are configured
- Forecast intervals are typically 30 minutes (varies by provider)
- Forecast data is cached for 15 minutes to avoid excessive API calls
- Timestamps are in UTC or local time depending on the pricing module
