# TWC Gen 3

## Introduction

This document describes TWCManager's support for the Tesla Wall Connector
Generation 3 and the approach used to control it.

> **Status: Supported** - Gen3 TWC control is implemented as of v1.4.0
> via the `Gen3TWCs` EVSEController.  See
> [Controller_Gen3TWCs.md](modules/Controller_Gen3TWCs.md) for
> configuration details.

---

## How Gen3 control works

The Gen3 TWC does **not** use the RS485 master/slave protocol that the
Gen2 TWC uses.  Instead, each Gen3 unit acts as a Modbus RTU *client*
that polls a [Neurio](https://www.neuriotech.com/) energy monitor to
measure how much headroom is available on the circuit, then
self-regulates its charge current to stay within that headroom.

TWCManager exploits this by running a **fake Neurio Modbus RTU server**
on the same serial bus.  To charge at a desired power level, it computes
a synthetic "house load":

```
house_load_watts = fuse_capacity_watts - target_charge_watts
```

and writes that value into the Neurio holding-register map.  The Gen3
reads the house load, subtracts it from its configured fuse capacity,
and charges at (up to) the resulting headroom - arriving at the target
power without any proprietary command protocol.

Actual charge state is read back from the Gen3's local HTTP API:

```
GET http://<gen3-ip>/api/1/vitals
```

which returns `vehicle_connected`, `vehicle_current_a`, `grid_v`,
`evse_state`, and related fields.

---

## Hardware requirements

| Component | Detail |
|-----------|--------|
| RS485 adapter | A **second** USB-to-RS485 adapter, separate from the one used for Gen2 slaves.  Connected to the Gen3's Neurio meter terminals. |
| Serial wiring | 2-wire RS485 between the adapter and the Gen3's Neurio header |
| Network | The Gen3 must be reachable by IP for vitals readback |
| Tesla account (optional) | To name the car on each Gen3, the Gen3 must be registered in the Tesla app, and TWCManager needs a token that can read energy data (a Fleet API token with the `energy_device_data` scope) |

> **Note:** The Gen3 has a single RS485 header (vs the pair on the Gen2).
> This header is used for the Neurio meter, not for TWC load-sharing.
> Gen2 and Gen3 units cannot share the same RS485 bus.

---

## Multiple Gen3 units

Multiple Gen3 TWCs sharing the same Neurio RS485 bus are supported.
Each unit is its own target, as a Gen2 TWC is: the distributor budgets
each unit (and the car on it) separately.  All units still poll the same
fake Neurio server and read the same house load, so TWCManager picks the
one house load at which the units with a target together draw the sum of
their targets.  A car that draws less than its headroom (for example, a
full battery) frees that share for the other units.  Units with
different fuse ratings can end up drawing differently from their own
targets.

In practice, a mixed Gen2+Gen3 deployment runs two RS485 buses:

- **Bus 1** (`/dev/ttyUSB0`, 9600 baud) - Gen2 TWC RS485 master/slave
- **Bus 2** (`/dev/ttyUSB1`, 115200 baud) - Neurio emulator for Gen3

---

## Which car is on a Gen3

The vitals API cannot say which car is connected.  A connect starts an
anonymous session, and TWCManager names the car by a free read of your
energy site's `live_status`, which lists each Wall Connector by DIN with
the VIN of the car on it.  This needs the Gen3 registered in the Tesla app
and a token that can read energy data.  A unit's optional `din` key fixes
a wrong DIN guess.  Until the energy site names the car, a single known
car that has just reported being plugged in is taken as the one at the
unit.  See [Controller_Gen3TWCs.md](modules/Controller_Gen3TWCs.md#which-car-is-connected)
for the rules.

---

## How a car on a Gen3 is commanded

- **Charge rate** - set only through the unit's headroom.  TWCManager sends
  no charge-rate command to a car on a Gen3, over BLE or the cloud.
- **Charge limit and wake** - handled like any other car, through the vehicle
  providers in order: local (Bluetooth LE) first, then the cloud.  The
  Vehicle Command Policy and `providerSuppressions` settings apply
  (see [Settings.md](Settings.md)).
- **Stopping one car** - not possible through the unit while another Gen3
  has a car connected; see Limitations.

---

## Limitations

- **No VIN reporting** - The Gen3 vitals API does not expose a vehicle
  VIN. A car is grouped into a Gen3 unit's charging point only when it is
  identified (from the energy site's wall connector list, or as the single
  freshly plugged-in candidate); otherwise the unit's car stays anonymous.
- **Indirect control** - Charge rate is set indirectly via the synthetic
  house load.  The Gen3's own minimum (6 A) and maximum (circuit limit)
  still apply and override TWCManager's target if it falls outside that
  range.
- **No individual stop on a shared bus** - All Gen3 units on the bus read
  one house-load figure.  Stopping a unit means presenting zero headroom,
  which is only possible when no other unit holds a target.  While more
  than one unit has a car connected, the Gen3 reports that it cannot stop
  one car individually (`NO_INDIVIDUAL_STOP`): zeroing one unit's target
  leaves that car free to keep drawing up to the shared headroom, and a
  stop for that car is carried out by the car's own providers (Bluetooth LE
  or the cloud), never through the Gen3 unit.  With a single occupied unit
  the stop works through zero headroom.
- **Unit-tested only** - Gen3 support has not been verified on hardware.
  Still unverified: that a zero headroom stops a unit, the shared headroom
  across several units, the `live_status` wall connector list (and its VIN
  while a car is connected), the DIN format (`<part_number>--<serial_number>`
  from `/api/1/version`), and how soon the energy site stops reporting a car
  that has left.

---

## Historical background

The Gen3 was released in January 2020.  For several years it was
incompatible with TWCManager because its RS485 pins were not activated
and it did not implement the Gen2 master/slave protocol.

Support became feasible once the community established that the Gen3
polls a Neurio meter over RS485 to measure circuit load, making it
possible to control indirectly by emulating that meter.  The TWCManager
implementation references the Neurio register map documented by the
community (no proprietary code used).

---

## Archived: pre-v1.4.0 status notes

The notes below are preserved for historical reference and reflect the
situation prior to v1.4.0.  They no longer represent the current state.

---

This document serves as a repository of what we do and don't know about the TWC Gen 3. Starting with the key detail:

### It was released in Jan 2020

   * At release time it had no support for integration with any Green Energy sources including Tesla's Powerwall.
      * As of June 2021, that continues to be the case

   * It was initially released without any power sharing support at all (the feature that TWCManager uses to track solar generation for the Gen 2)
      * See here: https://www.tesla.com/support/gen-3-wall-connector-power-sharing
      * As of June 2021, a firmware version (21.18.1) has been released which supports Power Sharing between Gen3 units (**only**) using Mesh WiFi. This does not enable the RS485 pins and does not provide backward compatibility with the Gen 2 Power Sharing feature.
      * Thankfully, there is hope RS485 will make it into a later firmware release, as Tesla state: 

**Can I connect Gen 2 and Gen 3 Wall Connectors together in a power sharing network?**

No. This configuration is not **currently** available.

*(emphasis mine)*

### It does not work with TWCManager

Unfortunately, the TWC Gen 3 is not currently compatible with TWCManager. In its current form, it does not appear that it could be. That said...

   * The Gen 3 TWC physically possesses a single (vs the pair in the TWC Gen 2) RX/TX terminal for RS485
      * It does not appear to be activated. Owners of Gen3 units have connected RS485 adapters to their Gen3 RS485 terminals and 

   * The Gen 3 TWC **can** be configured via WiFi, including the maximum amps for the unit, but this is a First Time installation wizard and becomes unavailable after a period of time (~ 5 minutes reportedly), so it does not appear to be useful for this purpose

### It has WiFi, so it can receive over the air (OTA) updates

Which is good. This means that it's **possible** that Tesla will add the functionality we need in the future

### We will be able to talk to TWC Gen 3 units

   * But it is all read-only values at this time. We have no endpoint that allows us to modify anything, currently.

## A plea about being informative

It's concerning to see a lot of statements nowadays stating that the TWC Gen 3 will contain the capability to charge based on solar consumption due to WiFi connectivity.

We sincerely hope it's the case and plan to support it as soon as it is available.

However, today, it is not available and we have even less control over a TWC Gen 3 than a Gen 2. This is not buying advice, perhaps the Gen 3 is overall the better solution for you, but please be aware that if you buy one today, we both are not able to support it currently, 

### What are our plans?

The plans are:

   * Assess the value of the current API endpoints - they appear to be valuable for monitoring (but not controlling) a Gen 3 TWC.
   * Wait for backward compatibility TWC Gen 2 support from Tesla, keeping in mind that Power Sharing took 16 months to make it into the firmware in its current form. This would allow integration with the Gen 3 without modification to the code, assuming Tesla fully implement the Gen 2 protocol.
   * Consider if we could participate in the WiFi Mesh
      * We just don't know. This is all too new, it was released this month and we're not even sure how the Gen 3s integrate yet.

## More detailed analysis

Below is an analysis of Firmware 21.18.1, looking for details around connectivity and API endpoints that we might consume:


  * /tedapi/v1
  * /tedapi/din


  * /access
  * /alerts
  * /api/1/lifetime
  * /api/1/version
  * /api/1/vitals

```
{
  "contactor_closed":false,
  "vehicle_connected":false,
  "session_s":0,
  "grid_v":232.0,
  "grid_hz":50.020,"vehicle_current_a":0.4,"currentA_a":0.2,"currentB_a":0.4,"currentC_a":0.2,"currentN_a":0.3,"voltageA_v":0.0,"voltageB_v":4.9,"voltageC_v":0.0,"relay_coil_v":12.0,"pcba_temp_c":29.0,"handle_temp_c":22.1,"mcu_temp_c":34.9,"uptime_s":190,"input_thermopile_uv":-204,"prox_v":0.0,"pilot_high_v":11.9,"pilot_low_v":11.9,"session_energy_wh":0.000,"config_status":5,"evse_state":1,"current_alerts":[]}
```

  * /api/1/wifi_status

```
{
  "wifi_ssid":"redacted",
  "wifi_signal_strength":36,
  "wifi_rssi":-72,
  "wifi_snr":22,
  "wifi_connected":true,
  "wifi_infra_ip":"192.168.xxx.xx",
  "internet":true,
  "wifi_mac":"xx:xx:xx:xx:xx:xx"
}
```

  * /error/public
  * /error/unauthenticated
  * /fwupdate
  * /installation
  * /service
  * /sharing
  * /sharing/add
  * /sharing/settings
  * /update (triggers firmware update)
  * /wifi

## Useful References

   * https://teslamotorsclub.com/tmc/threads/new-wall-connector-load-sharing-protocol.72830/page-26#post-4502118
   * https://www.tesla.com/support/gen-3-wall-connector-power-sharing
   * https://github.com/ngardiner/TWCManager/issues/292
   * https://teslamotorsclub.com/tmc/threads/gen3-wall-connector-api.228034/
