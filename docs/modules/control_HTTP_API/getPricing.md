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
    "export": 0.08,
    "currency": "$"
}
```

### Response Fields

| Field | Type | Description |
|-------|------|-------------|
| `import` | float | Current import price per kWh in local currency |
| `export` | float | Current export price per kWh in local currency |
| `currency` | string | Currency symbol reported by the active pricing module (empty if unknown) |

## Example

```bash
curl http://192.168.1.1:8080/api/getPricing
```

## Notes

- Prices are aggregated from all active pricing modules according to the `multiPrice` policy
- If no pricing modules are configured, both values will be `0`
- Negative import prices are possible with wholesale providers (eg Amber Electric)
- Values are refreshed periodically by the charging policy engine (default every 30 seconds)
