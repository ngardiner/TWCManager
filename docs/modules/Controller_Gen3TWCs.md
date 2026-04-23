# Gen3TWCs Controller

## Introduction

The `Gen3TWCs` EVSEController enables TWCManager to control Tesla Wall
Connector Generation 3 (Gen3) devices.

Gen3 TWCs self-regulate charge current by polling a
[Neurio](https://www.neuriotech.com/) energy monitor over Modbus RTU to
determine how much headroom is available on the circuit.  TWCManager
emulates that Neurio meter, writing a synthetic "house load" that creates
exactly the headroom needed to charge at the desired rate.

For background on the Gen3 and why this approach is necessary, see
[Gen3_Status.md](../Gen3_Status.md).

## Prerequisites

- **pymodbus >= 3.0.0** - installed automatically with `make install` or
  `pip install pymodbus`
- A **dedicated RS485 serial adapter** wired to the Gen3's Neurio meter
  terminals.  This must be a *separate* adapter from the one used for
  Gen2 RS485 master/slave communications.
- The Gen3 TWC must have its **external Neurio meter** option enabled at
  115200 baud (configured during installation or via the Gen3's own API).
- The Gen3 TWC must be **reachable by IP address** on the local network
  for charge-state readback via the HTTP vitals API.

## Configuration

Add the following to `config.json` and set `"enabled": true`:

```json
"controller.Gen3TWCs": {
    "enabled": true,

    "port": "/dev/ttyUSB1",
    "baudrate": 115200,
    "stopbits": 1,
    "bytesize": 8,
    "parity": "N",

    "devices": [
        {
            "ip": "192.168.1.100",
            "fuseAmps": 48,
            "phases": 1
        }
    ]
}
```

### Top-level fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Set to `true` to activate the controller |
| `port` | string | `/dev/ttyUSB1` | Serial device connected to the Gen3 Neurio meter RS485 bus |
| `baudrate` | int | `115200` | Baud rate - must match the Gen3's Neurio meter setting |
| `stopbits` | int | `1` | Stop bits |
| `bytesize` | int | `8` | Data bits |
| `parity` | string | `"N"` | Parity (`"N"` = none, `"E"` = even, `"O"` = odd) |
| `devices` | list | `[]` | One entry per Gen3 TWC on this bus |

### Per-device fields (`devices` list)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `ip` | string | - | IP address of the Gen3 TWC (for vitals readback) |
| `fuseAmps` | int | `48` | Circuit breaker rating x 0.8, in amps |
| `phases` | int | `1` | Number of AC phases (`1` for North America, `3` for most of Europe) |

> **fuseAmps** follows the same 80% derating convention as
> `wiringMaxAmpsAllTWCs`.  A 60 A breaker -> `"fuseAmps": 48`.

## How charge rate control works

When `setTargetPower(watts)` is called for a Gen3 device:

1. TWCManager computes the synthetic house load:
   ```
   house_watts = fuse_watts - target_watts
   ```
2. The house load is encoded as IEEE 754 FP32 values and written into
   the fake Neurio holding registers (total power at 0x90, per-phase at
   0x88/0x8A/0x8C, and corresponding current registers).
3. The Gen3 TWC polls those registers, reads the house load, subtracts
   it from its configured fuse capacity, and charges at the resulting
   headroom.

To **stop charging**, TWCManager writes `house_watts = fuse_watts`
(zero headroom), which causes the Gen3 to limit itself to 0 A.

## State readback

Charge state is read from the Gen3 HTTP vitals endpoint:

```
GET http://<ip>/api/1/vitals
```

Relevant response fields:

| Field | Used for |
|-------|----------|
| `vehicle_connected` | `wantsToCharge` |
| `evse_state` | `isCharging`, `wantsToCharge` |
| `vehicle_current_a` | `currentAmps` |
| `grid_v` | `currentVoltage` |

Vitals are cached for 5 seconds on success, 10 seconds on failure.  A
device that fails to respond is omitted from the active EVSE list for
that polling cycle.

## Multiple Gen3 units

Add one entry per unit to the `devices` list.  All units on the bus
share the same Neurio register image, so they each read the same house
load and share the available headroom equally.

```json
"devices": [
    { "ip": "192.168.1.100", "fuseAmps": 48, "phases": 1 },
    { "ip": "192.168.1.101", "fuseAmps": 48, "phases": 1 }
]
```

## Mixed Gen2 + Gen3 deployments

Gen2 and Gen3 units use different RS485 buses and are managed
independently.  Configure both:

```json
"interface": {
    "RS485": {
        "enabled": true,
        "port": "/dev/ttyUSB0",
        "baud": 9600
    }
},
"controller.Gen3TWCs": {
    "enabled": true,
    "port": "/dev/ttyUSB1",
    "baudrate": 115200,
    "devices": [ ... ]
}
```

TWCManager's centralized power distributor allocates watts across all
EVSE types (Gen2 slaves, Gen3 units, and Tesla API vehicles) using the
same fair-share algorithm.

## Limitations

- **No VIN** - The Gen3 vitals API does not report a vehicle VIN.
  `MergedEVSE` deduplication (used when a vehicle is visible via both a
  charger and the Tesla Fleet API) is not supported for Gen3 units.
- **Indirect control** - The Gen3's own hardware limits (6 A minimum,
  circuit maximum) override TWCManager's target if it falls outside that
  range.
- **Shared headroom** - Multiple Gen3 units on the same bus share one
  house-load figure; per-unit allocation is not currently possible.

## Troubleshooting

**Gen3TWCs controller fails to start / pymodbus import error**
: Install pymodbus: `pip install "pymodbus>=3.0.0"`

**Gen3 TWC does not respond to power changes**
: Verify the Gen3 is configured to use an external Neurio meter at 115200
baud.  Check that the correct serial port is configured and that the
RS485 wiring connects to the Neurio meter header (not the unused RS485
header).

**Vitals fetch always failing**
: Confirm the `ip` address is reachable: `curl http://<ip>/api/1/vitals`
from the machine running TWCManager.

**Gen3 charges at wrong rate**
: Check `fuseAmps` matches the circuit breaker x 0.8.  Check `phases`
matches your electrical installation.
