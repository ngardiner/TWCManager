# getCheapestWindow API Command

## Introduction

The getCheapestWindow API command finds the optimal time window for charging based on price forecasts. Useful for scheduling charging during the cheapest hours. Requires a pricing module with forecasting capability (e.g., Amber).

## Format of Request

```
GET /api/getCheapestWindow?hours=4&startHour=22&endHour=6
```

### Query Parameters

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `hours` | no | 4 | Number of consecutive hours needed (1-24) |
| `startHour` | no | current | Earliest hour to start charging (0-23) |
| `endHour` | no | none | Latest hour to finish charging (0-23). If less than startHour, window crosses midnight. |

## Format of Response

**Success:**
```json
{
    "startHour": 1,
    "startTimestamp": "2025-01-16T01:00:00",
    "avgPrice": 0.12,
    "totalCost": 0.48
}
```

**No suitable window found:**
```json
{
    "error": "No suitable window found",
    "numHours": 4,
    "startHour": 22,
    "endHour": 6
}
```

### Response Fields

| Field | Type | Description |
|-------|------|-------------|
| `startHour` | integer | Best hour to start charging (0-23) |
| `startTimestamp` | string | ISO 8601 timestamp for recommended start time |
| `avgPrice` | float | Average price for the window ($/kWh) |
| `totalCost` | float | Estimated total cost for the charging session |

## Example

```bash
# Find cheapest 4-hour window anytime
curl http://192.168.1.1:8080/api/getCheapestWindow?hours=4

# Find cheapest 5-hour window overnight (10 PM to 6 AM)
curl "http://192.168.1.1:8080/api/getCheapestWindow?hours=5&startHour=22&endHour=6"

# Find cheapest 3-hour window during daytime (9 AM to 5 PM)
curl "http://192.168.1.1:8080/api/getCheapestWindow?hours=3&startHour=9&endHour=17"
```

## Notes

- Returns `null` or error if no forecasting-capable modules are available
- Window crossing midnight: If `endHour < startHour`, the search window spans midnight (e.g., 22:00 to 06:00)
- The algorithm finds the contiguous window with the lowest average price
- Results are based on forecast data and may differ from actual prices

## Use Case: Smart Overnight Charging

Combine with scheduled charging or automation to charge at the optimal time:

```python
# Example automation script
import requests
import datetime

# Get cheapest 5-hour window between 10 PM and 6 AM
response = requests.get('http://localhost:8080/api/getCheapestWindow?hours=5&startHour=22&endHour=6')
result = response.json()

if 'startHour' in result:
    print(f"Best time to start charging: {result['startHour']}:00")
    print(f"Average price: ${result['avgPrice']}/kWh")
    print(f"Estimated cost: ${result['totalCost']}")
```
