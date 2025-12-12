# Home Assistant Tesla Vehicle Integration

## Introduction

The **Home Assistant Tesla Vehicle Integration** allows TWCManager to talk to your Teslas **via Home Assistant** instead of using the direct Tesla API.

This is useful if:

* You already have the **`tesla_fleet`** integration in Home Assistant
* Your Teslas are fully configured and working in HA (entities updating correctly)
* You'd prefer *not* to manage Tesla OAuth, tokens, or HTTP proxies directly in TWCManager

With this integration, TWCManager will:

* Automatically discover all **Tesla vehicles** in Home Assistant
* Use the **entities exposed by `tesla_fleet`** to:
  * read battery level, charge status, charger current/voltage, time-to-full, and location
  * start and stop charging
  * set charge limits
  * set charge current (amps)

No polling of `vehicle_data` from Tesla is done. All communication is with **Home Assistant's API only**.

## Requirements

You need:

1. A working **Home Assistant** instance.
2. The **Tesla Fleet (`tesla_fleet`) integration** configured in Home Assistant, with:
   * Each Tesla exposed as a device with:
     * manufacturer `Tesla`
     * model like `Model 3`, `Model Y`, `Model X`, `Model S`
     * a **serial number** equal to the VIN (e.g. `5YJ3F7EA4KF449191`)
   * Entities such as:
     * `sensor.<car>_battery_level`
     * `sensor.<car>_charging`
     * `switch.<car>_charge`
     * `number.<car>_charge_current`
     * `number.<car>_charge_limit`
     * `device_tracker.<car>_location`
3. A **Home Assistant long-lived access token** for API access.

Once this is set up, TWCManager can use Home Assistant to communicate with the car and no longer needs direct access to the Tesla API for charging control.

## How It Works

### Discovery via Home Assistant

On startup, the Home Assistant vehicle module:

1. Connects once to the **Home Assistant WebSocket API** (`/api/websocket`)
2. Authenticates with the long-lived token
3. Requests:
   * `config/device_registry/list`
   * `config/entity_registry/list`
4. For each device, it looks for:
   * `manufacturer` or `default_manufacturer` containing `"Tesla"`
   * `model` in a known list (e.g. `Model 3`, `Model Y`, `Model X`, `Model S`)
   * a **VIN-like serial number**, matching the standard 17-character VIN format
5. For each matching Tesla device, it:
   * Derives a **slug** from the name (for example:
     * `Batmobile` → `batmobile`
     * `Canberra Adventurer` → `canberra_adventurer`)
   * Finds the `tesla_fleet` entities related to that device, matching patterns like:
     * `sensor.<slug>_battery_level`
     * `sensor.<slug>_charging`
     * `switch.<slug>_charge`
     * `number.<slug>_charge_current`
     * `number.<slug>_charge_limit`
     * `device_tracker.<slug>_location`

### Reading Vehicle State

After discovery, **ongoing interaction uses the Home Assistant REST API only**:

* `GET /api/states/<entity_id>` for:
  * battery level (`sensor.<slug>_battery_level`)
  * charging state (`sensor.<slug>_charging`)
  * time to full (`sensor.<slug>_time_to_full_charge`)
  * charger current and voltage
  * multi-phase information (`binary_sensor.<slug>_charger_has_multiple_phases`)
  * location (`device_tracker.<slug>_location`)

The module uses this data to:

* populate each vehicle's:
  * `battery_level`
  * `charging_state` / `charging`
  * `time_to_full_charge`
  * `available_current` / `actual_current`
  * `voltage` and `phases`
  * `lat`, `lon`, and `at_home`
* expose aggregate properties to TWCManager such as:
  * `numCarsAtHome`
  * `minBatteryLevelAtHome`

Home location and "at home radius" are taken from the main TWCManager configuration, using the same heuristic as the original Tesla API module (with the `atHomeRadius` setting in feet).

### Charging Control

To control charging, the module calls **Home Assistant services**:

* **Start charging**
  → `POST /api/services/switch/turn_on` with `{"entity_id": "switch.<slug>_charge"}`
* **Stop charging**
  → `POST /api/services/switch/turn_off` with `{"entity_id": "switch.<slug>_charge"}`
* **Set charge limit (%)**
  → `POST /api/services/number/set_value` with
    `{"entity_id": "number.<slug>_charge_limit", "value": <limit>}`
* **Set max charge current (amps)**
  → `POST /api/services/number/set_value` with
    `{"entity_id": "number.<slug>_charge_current", "value": <amps>}`

TWCManager's existing logic (like `applyChargeLimit`, `car_api_charge`, `setChargeRate`) is wired to call these Home Assistant services instead of the direct Tesla API.

## TWCManager Configuration

In your `config.json`, enable the Home Assistant vehicle integration:

```json
{
  "vehicle": {
    "HomeAssistant": {
      "enabled": true,
      "url": "http://homeautomation.lan:8123",
      "longLivedToken": "abcdef123456-your-ha-token-here"
    }
  }
}
```

**Fields:**

* `enabled`
  * `true` to enable this integration, `false` to disable.
* `url`
  * The base URL for your Home Assistant instance.
  * Example: `"http://homeautomation.lan:8123"` or `"https://ha.example.com"`
* `longLivedToken`
  * A **Home Assistant long-lived access token** with permission to:
    * read device and entity registries
    * read states
    * call `switch` and `number` services

You can generate a long-lived token in Home Assistant under:
**Profile → Long-Lived Access Tokens → Create Token**.

## Behaviour & Fallbacks

* If the module is enabled but no Tesla vehicles are discovered:
  * It logs a warning and exposes no cars to TWCManager.
* If Home Assistant is temporarily unreachable:
  * `car_api_available()` returns `False`, and TWCManager will not attempt charge actions through this backend until connectivity is restored.
* If a specific entity is missing (e.g. `number.<slug>_charge_current`):
  * That feature (e.g. setting amps) is skipped, but other features (start/stop, limit, SOC) can still work if their entities exist.

The model is deliberately conservative: it won't try to use or guess entities that it did not positively match based on the slug and the `tesla_fleet` platform.

## When to Use This Integration

Use the **Home Assistant Tesla Vehicle Integration** when:

* You already maintain Tesla integration in Home Assistant and want a **single source of truth**
* You don't want to manage Tesla OAuth directly in TWCManager
* You want to keep your Tesla access scoped to **Home Assistant**, and let TWCManager only talk to Home Assistant's APIs
* You're comfortable having Home Assistant as the "brain" that knows about your cars, and TWCManager as the "charger controller" that uses this data

If you later add more Teslas to Home Assistant, they will be **auto-discovered** by this integration on the next TWCManager restart (as long as they follow the same `tesla_fleet` naming patterns and expose a VIN).
