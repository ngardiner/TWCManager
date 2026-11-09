# getPricingForecast API Command

## Introduction

The getPricingForecast API command returns the forward price forecast from each
loaded Pricing module that can forecast prices (Amber, Tesla).
Modules without forecasting are omitted from the response.

## Format of request

An optional `hours` query parameter bounds the forecast horizon (1-48, default
24).

```
GET /api/getPricingForecast?hours=24
```

An example of how to call this function via cURL is:

```
curl -X GET "http://192.168.1.1:8080/api/getPricingForecast?hours=24"
```

## Format of response

```json
{
    "hoursRequested": 24,
    "modules": {
        "AmberPricing": {
            "currency": "$",
            "cheapWindowActive": 1,
            "cheapWindowStart": "2026-01-15T22:00:00+00:00",
            "forecast": [
                {
                    "timestamp": "2026-01-15T14:00:00+00:00",
                    "importPrice": 0.32,
                    "descriptor": "neutral",
                    "spikeStatus": "none",
                    "renewables": 45
                }
            ]
        }
    }
}
```

`cheapWindowActive` is `1` while the current time is inside the cheapest window
the module found for its configured `cheapWindow`; it is `0` (and
`cheapWindowStart` is `null`) when no `cheapWindow` is configured. See
[Pricing Modules](../Pricing.md#charging-in-the-cheapest-window).
