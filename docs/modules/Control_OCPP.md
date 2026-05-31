# OCPP Control Module

## Introduction

The OCPP Control Module implements an OCPP 1.6 Central System (server). OCPP-capable charge points can connect to TWCManager over WebSocket, allowing TWCManager to receive charge session events and send remote start/stop and charging profile commands.

This is useful for integrating future hardware that speaks OCPP natively (such as later generations of the Tesla Wall Connector), or for bridging TWCManager into a broader OCPP-based energy management ecosystem.

### Status

| Detail          | Value        |
| --------------- | ------------ |
| **Module Name** | OCPPControl  |
| **Module Type** | Control      |
| **Protocol**    | OCPP 1.6J    |
| **Role**        | Central System (server) |
| **Status**      | Beta         |

### Dependencies

| Package       | Version  | Notes |
| ------------- | -------- | ----- |
| ocpp          | >=2.0    | OCPP protocol library |
| websockets    | >=10.0   | WebSocket transport |

Install via: `pip install ocpp>=2.0 websockets>=10.0`

## Configuration

| Parameter    | Value |
| ------------ | ----- |
| enabled      | *required* `true` or `false` |
| serverPort   | *optional* WebSocket listen port. Default: `9000` |
| serverHost   | *optional* Interface to bind. Default: `0.0.0.0` (all interfaces) |

### JSON Configuration Example

```json
"control": {
  "OCPP": {
    "enabled": true,
    "serverPort": 9000,
    "serverHost": "0.0.0.0"
  }
}
```

## Charge Point Connection

Charge points connect using the standard OCPP 1.6J WebSocket URL format:

```
ws://<twcmanager-host>:<port>/<charge-point-id>
```

Example: `ws://192.168.1.10:9000/charger-01`

The charge point must negotiate the `ocpp1.6` subprotocol.

## Supported OCPP Messages

### Incoming (Charge Point -> TWCManager)

| Message              | Behaviour |
| -------------------- | --------- |
| BootNotification     | Accepts all charge points (returns `Accepted`) |
| Heartbeat            | Responds with current UTC time |
| Authorize            | Accepts all id tags |
| StatusNotification   | Records connector status |
| StartTransaction     | Records transaction, assigns transaction ID |
| StopTransaction      | Clears transaction record |
| MeterValues          | Logs sampled values; tracks `Power.Active.Import` per connector |

### Outgoing (TWCManager -> Charge Point)

| Message                | Trigger |
| ---------------------- | ------- |
| RemoteStartTransaction | `sendStartCharge()` |
| RemoteStopTransaction  | `sendStopCharge()` |
| SetChargingProfile     | `sendChargeRate(amps)` - sets a `TxDefaultProfile` to cap charge current |

## Programmatic Control

Other TWCManager modules can interact with connected charge points via the OCPPControl module:

```python
ocpp = master.getModuleByName("OCPPControl")

# List connected charge points
ocpp.connectedChargePoints()        # -> ["charger-01", ...]

# Start/stop a transaction
ocpp.sendStartCharge(cp_id="charger-01", connector_id=1)
ocpp.sendStopCharge(cp_id="charger-01", connector_id=1)

# Set charge rate
ocpp.sendChargeRate(amps=16, cp_id="charger-01", connector_id=1)

# Query connector status
ocpp.chargePointStatus("charger-01", connector_id=1)
```

Passing `cp_id=None` broadcasts the command to all connected charge points.
