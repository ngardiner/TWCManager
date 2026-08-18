# Amber Electric Pricing Module

## Introduction

The Amber Electric Pricing module fetches real-time electricity prices from [Amber Electric](https://amber.com.au/), an Australian energy retailer that passes through wholesale market prices. Prices update every 30 minutes and can vary significantly - even going negative during high solar generation periods.

### Status

| Detail            | Value                                           |
| ----------------- | ----------------------------------------------- |
| **Module Name**   | AmberPricing                                    |
| **Module Type**   | Pricing                                         |
| **Features**      | Import price, Export price, Spike detection, Renewables, **Forecasting** |
| **Status**        | Implemented, Stable                             |
| **Region**        | Australia (NSW, VIC, QLD, SA)                  |

### Key Features

Amber Electric provides unique features beyond basic pricing:

- **Price Spikes**: Get notified when prices spike dramatically (often 10x normal rates)
- **Negative Prices**: Prices can go negative during high renewable generation - get paid to charge!
- **Export Pricing**: Real-time feed-in tariffs for solar/battery exports
- **Renewables Percentage**: See how much of the grid is powered by renewables
- **Price Descriptors**: Human-readable price levels (extremelyLow, veryLow, low, neutral, high, spike)
- **Price Forecasting**: Access future price predictions for smart scheduling
- **Cheapest Window Finder**: Automatically identify the cheapest time to charge

## Configuration

```json
"pricing": {
    "Amber": {
        "enabled": true,
        "token": "your-amber-api-token-here",
        "siteId": "01F5A5CRKMZ5BCX9P1S4V990AM"
    }
}
```

### Parameters

| Parameter   | Value |
| ----------- | ----- |
| `enabled`   | *required* `true` or `false` |
| `token`     | *required* API token from Amber Electric developer portal |
| `siteId`    | *optional* Site identifier. If omitted, auto-discovered from your account |
| `cacheTime` | *optional* Cache duration in seconds (default: 300 = 5 minutes) |

### Getting an API Token

1. Log in to your Amber Electric account
2. Navigate to the developer/API section
3. Generate an API token

## Policy Integration

### Basic Price Threshold

Stop charging when import price exceeds a threshold:

```json
"config": {
    "maxImportPrice": 0.40
},
"policy": {
    "extend": {
        "emergency": [
            {
                "name": "Import Price Too High",
                "match": ["getImportPrice()"],
                "condition": ["gt"],
                "value": ["config.maxImportPrice"],
                "charge_amps": 0
            }
        ]
    }
}
```

### Charge on Negative Prices

Charge when prices go negative (get paid to charge!):

```json
"policy": {
    "extend": {
        "emergency": [
            {
                "name": "Negative Price Charging",
                "match": ["getImportPrice()"],
                "condition": ["lt"],
                "value": [0],
                "charge_amps": "config.nonScheduledAmpsMax"
            }
        ]
    }
}
```

### Solar Export Arbitrage

Stop charging when export price exceeds import price (sell instead of charge):

```json
"policy": {
    "extend": {
        "emergency": [
            {
                "name": "Export Price Favourable",
                "match": ["getExportPrice()"],
                "condition": ["gt"],
                "value": ["getImportPrice()"],
                "charge_amps": 0
            }
        ]
    }
}
```

## Advanced Features

### Spike Detection

The module exposes spike status via `getSpikeStatus()`:

- `none` - Normal pricing
- `potential` - Price spike possible in upcoming intervals
- `spike` - Price spike currently occurring

When a spike is detected, the module logs a warning at debug level 5.

### Price Descriptors

Human-readable price levels via `getPriceDescriptor()`:

| Descriptor     | Meaning                          |
| -------------- | -------------------------------- |
| `extremelyLow` | Very cheap (often negative)      |
| `veryLow`      | Below average                    |
| `low`          | Slightly below average           |
| `neutral`      | Around average                   |
| `high`         | Above average                    |
| `spike`        | Significantly above average      |

### Renewables Percentage

Grid renewable percentage (0-100) via `getRenewables()`. Can be used for green charging strategies.

## Forecasting (NEW)

### Price Forecasts

The module can fetch price forecasts for future intervals using `getPriceForecast(hoursAhead)`:

```python
# Returns list of forecast entries
forecast = master.getPriceForecast(hoursAhead=12)

# Each entry contains:
# {
#     "timestamp": datetime,
#     "importPrice": 0.25,      # $/kWh
#     "exportPrice": 0.08,      # $/kWh
#     "spikeStatus": "none",    # none/potential/spike
#     "descriptor": "neutral",  # price level
#     "renewables": 45          # percentage
# }
```

### Cheapest Window Scheduling

Find the optimal time to charge using `getCheapestWindow(numHours, startHour, endHour)`:

```python
# Find cheapest 5-hour charging window overnight
result = master.getCheapestWindow(
    numHours=5,      # Need 5 hours of charging
    startHour=22,    # After 10 PM
    endHour=6        # Before 6 AM (crosses midnight)
)

# Returns:
# {
#     "startHour": 1,           # Start at 1 AM
#     "startTimestamp": datetime,
#     "avgPrice": 0.12,         # Average $/kWh for window
#     "totalCost": 0.60         # Estimated total cost
# }
```

### Use Case: Smart Overnight Charging

Instead of charging immediately, schedule charging for the cheapest window:

```python
# In custom policy or automation:
cheapest = master.getCheapestWindow(numHours=5, startHour=22, endHour=6)

if cheapest and current_hour == cheapest["startHour"]:
    # Start charging at optimal time
    master.setMaxAmpsToDivideAmongSlaves(max_amps)
else:
    # Wait for optimal window
    master.setMaxAmpsToDivideAmongSlaves(0)
```

## Price Format

Prices are returned in **AUD/kWh** (Australian Dollars per kilowatt-hour). The Amber API returns prices in cents/kWh; this module converts to dollars/kWh for consistency.

## Rate Limits

Amber Electric API has rate limits. The module caches prices for 5 minutes by default (configurable via `cacheTime`). Forecast data is cached for 15 minutes. Avoid setting cacheTime below 60 seconds.

## Dashboard

When any Pricing module is active, the current import and export prices are displayed on the main dashboard and refreshed every 30 seconds.

## Multiple Pricing Modules

If using Amber alongside other pricing modules, configure the `multiPrice` policy:

```json
"pricing": {
    "policy": {
        "multiPrice": "first"
    },
    "Amber": {
        "enabled": true,
        "token": "your-token"
    }
}
```

## Capability Detection

The module reports capabilities via `getCapabilities()`:

- `AdvancePricing` - Always `True`
- `SpikeDetection` - Supports spike status
- `Renewables` - Provides renewable percentage
- `Forecasting` - Supports price forecasting and window scheduling

## API Endpoints

The following API endpoints are available for pricing integration:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/getPricing` | GET | Returns current import and export prices |
| `/api/getPricingModules` | GET | Lists all configured pricing modules with status |
| `/api/getPricingDetails` | GET | Detailed info for modules (all or `?module=Amber`) |
| `/api/getPricingForecast` | GET | Price forecast (`?hours=24`) |
| `/api/getCheapestWindow` | GET | Find optimal charging window (`?hours=4&startHour=22&endHour=6`) |
| `/api/refreshPricing` | POST | Force refresh pricing data |

### Example API Responses

**GET /api/getPricing**
```json
{
    "import": 0.35,
    "export": 0.12
}
```

**GET /api/getPricingModules**
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
        "exportPrice": 0.12
    }
]
```

**GET /api/getPricingDetails?module=Amber**
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

**GET /api/getPricingForecast?hours=12**
```json
{
    "hoursRequested": 12,
    "forecast": [
        {
            "timestamp": "2025-01-15T14:00:00",
            "importPrice": 0.32,
            "exportPrice": 0.10,
            "spikeStatus": "none",
            "descriptor": "neutral",
            "renewables": 45
        }
    ]
}
```

**GET /api/getCheapestWindow?hours=4&startHour=22&endHour=6**
```json
{
    "startHour": 1,
    "startTimestamp": "2025-01-16T01:00:00",
    "avgPrice": 0.12,
    "totalCost": 0.48
}
```

## Web UI

When Amber (or any pricing module) is configured, a **Pricing** link appears in the navigation bar between Policy and Schedule. The dedicated pricing page provides:

### Main Dashboard Enhancement
- Import and export prices (refreshed every 30 seconds)
- Price level descriptor (e.g., "neutral", "high", "spike")
- Renewables percentage (when available)

### Pricing Page Features

**Module Overview**
- Active pricing modules with current prices
- Capability badges (Spike Detection, Renewables, Forecasting)
- Price level color coding:
  - 🟢 Green: Low prices (below 70% of average)
  - 🟡 Yellow: Medium prices
  - 🔴 Red: High prices (above 130% of average)

**Price Spike Alerts**
- Visual warning banner when prices spike
- Spike status indicator per module

**Price Forecast Table**
- 24-hour price forecast (when module supports forecasting)
- Import/export prices per 30-minute interval
- Renewables percentage per interval
- Cheapest intervals highlighted
- Spike warnings displayed

**Optimal Charging Window Finder**
- Interactive form to find cheapest charging times
- Specify hours needed and time window constraints
- Handles overnight windows (e.g., 22:00 to 06:00)
- Returns best start hour and estimated cost

### UI Access Control

The Pricing link in the navbar is only shown when at least one pricing module is configured and enabled. If no pricing modules are active, accessing `/pricing` redirects to the home page.
