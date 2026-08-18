# getPricing API Command

## Introduction

The getPricing API command returns the current electricity import and export prices from all active pricing modules.

## Format of Request

```
GET /api/getPricing
```

## Format of Response

```json
{
    "import": 0.25,
    "export": 0.08
}
```

### Response Fields

| Field | Type | Description |
|-------|------|-------------|
| `import` | float | Current import price per kWh in local currency |
| `export` | float | Current export price per kWh in local currency |

## Example

```bash
curl http://192.168.1.1:8080/api/getPricing
```

## Notes

- Prices are aggregated from all active pricing modules according to the `multiPrice` policy
- If no pricing modules are configured, both values will be `0`
- Prices are cached and refreshed periodically (interval depends on module configuration)
