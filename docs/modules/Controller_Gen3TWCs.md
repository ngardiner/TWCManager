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
| `din` | string | - | Optional.  The unit's DIN as Tesla reports it, if TWCManager's guess is wrong (see [Which car is connected](#which-car-is-connected)) |

> **fuseAmps** follows the same 80% derating convention as
> `wiringMaxAmpsAllTWCs`.  A 60 A breaker -> `"fuseAmps": 48`.

## How charge rate control works

Each Gen3 unit gets its own target, as a Gen2 TWC does.  Every unit on
the bus reads the same fake Neurio value, so TWCManager turns all the
targets into one synthetic house load:

1. It picks the house load (in amps per phase) at which the units with a
   target together draw the sum of their targets.  With one unit, the
   headroom is exactly that unit's target, as before.  With several units
   and equal fuses, each gets the average of the targets.  If a car is
   drawing less than its headroom (for example, a full battery), its
   unused share goes to the other units.
2. The house load is encoded as IEEE 754 FP32 values and written into
   the fake Neurio holding registers (total power at 0x90, per-phase at
   0x88/0x8A/0x8C, and corresponding current registers).
3. Each Gen3 TWC polls those registers, subtracts the house load from its
   own configured fuse capacity, and charges at up to the resulting
   headroom.

Every unit with a target always keeps at least 6 A of headroom.  Small
changes in what the cars draw (under 1 A) do not change the published
value.

To **stop charging**, TWCManager presents the full fuse as house load
(zero headroom).  It can only do that when no other unit holds a target;
see [Multiple Gen3 units](#multiple-gen3-units).

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

Vitals are fetched in the background and cached for 5 seconds on
success, 10 seconds on failure; the charging loop only ever reads the
cache, so a slow or unreachable unit never delays it.  A unit that fails
to respond is left out of the active EVSE list until a fetch succeeds.

### Which car is connected

The vitals report that *a* car is connected, not which one.  When a car
connects, TWCManager starts an anonymous session for that unit, which it
can control like any other car.  It then tries to name the car:

- Tesla's cloud says which car is connected.  TWCManager asks once when
  the car connects, then once a minute until the cloud names the car or
  it is unplugged (every 10 minutes after 10 tries that name no car).  A
  car TWCManager has not seen before is named once the cloud reports the
  same VIN twice.  The car that just left this unit is not accepted until
  a minute after the next car connects, in case the cloud has not yet
  noticed it left.
- Until then, if exactly one known car reported being plugged in within
  the last two minutes, is not already on another TWC, and is not known
  to be away from home, that car is taken to be the one at this unit.
  This is a guess: if the cloud later names a different car, that car
  replaces it.
- If neither names the car, the session stays anonymous until the car
  is unplugged.

The cloud lookup is free: it reads your energy site's live status, never
the car, so it never wakes the car or uses paid Fleet API data.  It
needs:

- The Gen3 registered in the Tesla app.
- A Tesla API token that can read energy data: a Fleet API token with
  the `energy_device_data` scope.  A TeslaMate (Owner API) token is tried
  first and may also work; if Tesla refuses it, TWCManager uses the Fleet
  token.
- The unit's DIN.  TWCManager reads it from the unit
  (`http://<ip>/api/1/version`, as `<part_number>--<serial_number>`).  If
  the log shows `no DIN match` for your unit, set its `din` key to the
  DIN Tesla reports for the unit.  With only one Gen3 and one Wall
  Connector on the account, no DIN is needed.

The cloud lookup has not yet been tested on real hardware.

## Multiple Gen3 units

Add one entry per unit to the `devices` list.  All units on the bus
share the same Neurio register image, so they each read the same house
load.  TWCManager sets that load so the units' total draw matches the
sum of their targets (see above).  Each unit's own draw can differ from
its own target, especially when the units have different `fuseAmps`.

Because the value is shared, one unit cannot be stopped while another
charges: a unit with a zero target can still draw up to the shared
headroom.  TWCManager reports this as a limitation of the Gen3 control
path whenever more than one unit has a car connected, so it can stop
that car another way (for example, over Bluetooth or the Fleet API).

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

- **No local VIN** - The Gen3 vitals API does not report a vehicle VIN.
  A car is identified by the rules in [Which car is
  connected](#which-car-is-connected).  The plug-in rule can mistake one
  car for another: a car known only through TeslaMate has no "away from home"
  check, and a car plugging into a Gen2 TWC at the same moment is a
  candidate until the Gen2 reads its VIN.  The cloud lookup corrects
  such a guess when it names the car.
- **Indirect control** - The Gen3's own hardware limits (6 A minimum,
  circuit maximum) override TWCManager's target if it falls outside that
  range.
- **Shared headroom** - Multiple Gen3 units on the same bus share one
  house-load figure.  Their total follows the sum of the targets, but no
  unit can be stopped alone while another charges.
- **Untested on hardware** - Multi-unit sharing and the zero-headroom
  stop have not yet been verified on real Gen3 units.

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
