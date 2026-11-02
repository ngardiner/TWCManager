# Victron CerboGX EMS Module

## Introduction

Victron GX devices (such as the CerboGX) expose the state of a Victron energy
system over Modbus TCP. This module reads per-phase power values from the GX
device to provide Consumption and Generation information to TWCManager.

By default the module reads the system-bus registers: per-phase AC Output Power
(treated as Consumption) and per-phase AC-coupled PV Output (treated as
Generation). Because both the register addresses and the Modbus unit (device)
IDs are configurable, the module can instead be pointed at a specific meter on
the Modbus map — for example an individual grid meter — without code changes.

### Note

Victron publishes the full CCGX / GX Modbus-TCP register list (the
"CCGX-Modbus-TCP-register-list" spreadsheet) on the Victron website. The
registers you need depend on how your system is wired (the unit ID of the
system bus versus an individual meter, and whether PV is AC- or DC-coupled). If
the default registers do not report the values you expect, consult that list
and override `consumptionRegisters` / `generationRegisters` in your
configuration.

The module caches readings for 10 seconds to limit Modbus traffic; TWCManager
polling more frequently than that will reuse the cached value.

### Status

| Detail          | Value                          |
| --------------- | ------------------------------ |
| **Module Name** | Victron                        |
| **Module Type** | Energy Management System (EMS) |
| **Features**    | Consumption, Generation        |
| **Status**      | Implemented, Tested            |

## Requirements

This module requires the `pyModbusTCP` Python package, and Modbus TCP must be
enabled on the GX device (Settings → Services → Modbus TCP).

## Configuration

The following table shows the available configuration parameters for the
Victron EMS module.

| Parameter              | Value |
| ---------------------- | ----- |
| enabled                | *required* Boolean value, `true` or `false`. Determines whether we will poll the Victron device. |
| serverIP               | *required* The IP address of the Victron GX device. |
| serverPort             | *optional* Modbus TCP port. This is almost always `502`. |
| unitID                 | *optional* Default Modbus unit (device) ID. `100` is the GX system bus. |
| timeout                | *optional* Modbus connection timeout in seconds (default `10`). |
| consumptionRegisters   | *optional* List of `[register, unitID]` pairs read and summed for Consumption. Defaults to the system-bus per-phase Output Power registers (817, 818, 819 on unit 100). |
| generationRegisters    | *optional* List of `[register, unitID]` pairs read and summed for Generation. Defaults to the system-bus per-phase AC-coupled PV Output registers (808, 809, 810 on unit 100). |

### JSON Configuration Example

Default (system-bus) configuration:

```
"Victron": {
  "enabled": true,
  "serverIP": "192.168.1.2",
  "serverPort": 502,
  "unitID": 100,
  "timeout": 10
}
```

Overriding the registers (for example, to read a different meter):

```
"Victron": {
  "enabled": true,
  "serverIP": "192.168.1.2",
  "consumptionRegisters": [ [817, 100], [818, 100], [819, 100] ],
  "generationRegisters":  [ [808, 100], [809, 100], [810, 100] ]
}
```
