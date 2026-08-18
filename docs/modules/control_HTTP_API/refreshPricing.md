# refreshPricing API Command

## Introduction

The refreshPricing API command forces an immediate refresh of pricing data from all active pricing modules. Useful for getting the latest prices without waiting for the cache to expire.

## Format of Request

```
POST /api/refreshPricing
```

No request body required.

## Format of Response

```json
{
    "status": "success",
    "import": 0.35,
    "export": 0.12
}
```

### Response Fields

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | `success` if refresh completed |
| `import` | float | Current import price after refresh |
| `export` | float | Current export price after refresh |

## Example

```bash
curl -X POST http://192.168.1.1:8080/api/refreshPricing
```

## Notes

- Forces all pricing modules to refresh their cached data
- May trigger API calls to external services (respecting rate limits)
- Returns the current prices after refresh
- Useful before making charging decisions based on pricing
- Normal cache behavior resumes after refresh

## Use Cases

### Before Critical Charging Decision

```bash
# Refresh prices before deciding whether to charge
curl -X POST http://192.168.1.1:8080/api/refreshPricing
```

### Automation Script

```python
import requests

# Force price refresh and get latest values
response = requests.post('http://localhost:8080/api/refreshPricing')
data = response.json()

if data['status'] == 'success':
    print(f"Updated prices - Import: ${data['import']}/kWh, Export: ${data['export']}/kWh")
```
