# RS485 Communication Interface

## Introduction

The RS485 Interface Module provides Serial and (limited) Network connectivity between TWCManager and Slave TWCs.

You should use this module if:

   * Your device is directly connected to the TWC devices via a physical RS485 (2-wire) connection, either via USB device, shield, etc.
   * Your device connects over the network to an RS485 to TCP converter which is supported via the Raw or RFC2217 (Telnet) protocols without encryption.

## Serial

Serial communications with TWC is established through the use of a Serial Device, such as the default setting of ```/dev/ttyUSB0``` which represents the first USB to Serial device connected to a machine.

Some examples of Serial configuration include:

```"port": "/dev/ttyUSB0"``` - Connection to a USB to RS485 adaptor
```"port": "/dev/ttyS0"```   - Connection to an onboard Serial interface

If the adapter is missing or cannot be opened, TWCManager logs one error and tries again every 30 seconds, so an adapter plugged in late is picked up without a restart (1.4.1). A Gen3-only or car-only install can leave the RS485 settings as shipped. The RS485 interface stands down if its port is the one the Gen3 controller uses.

## Network Communications

There are two different network protocols which are supported by the RS485 module. These are less configurable than the alternate planned TCP module, however the TCP module is not yet available for use.

   * rfc2217 - Telnet to Serial
   * socket  - Raw network to Serial

Generally, a device will provide documentation which describes the network encoding used. 

Examples of the configuration of these protocols:

```"port": "rfc2217://192.168.1.2:4000/"```

```"port": "socket://192.168.1.2:4000/"```

## Per-TWC amps range

By default every slave TWC is offered between minAmpsPerTWC (never less than the TWC itself supports) and its wiring maximum. The distributor gives an idle TWC a target of 0 and counts nothing against the budget. On every control tick (about once a second) the TWC is offered 0 when no car is charging (a Gen2 treats 0 as its hardware minimum); when another car is charging and there is at least minAmpsPerTWC of power per car, it is offered its hardware minimum (usually 6 A), so it can see a car plug in. Brief overshoot is fine: the configured limit is already the 80% continuous rating and the next cycle rebalances. A charging TWC whose share would fall below its minimum is stopped (offered 0). To set a different range for one TWC, add `controller.Gen2TWCs` to config.json:

```json
"controller.Gen2TWCs": {
    "ranges": {
        "8244": { "minAmps": 8, "maxAmps": 16 }
    }
}
```

The key is the TWC's ID as four hex digits, as the log and web page show it. `minAmps` replaces minAmpsPerTWC for that TWC, but never goes below what the TWC supports. `maxAmps` caps what the power distributor offers it; wiringMaxAmpsPerTWC and wiringMaxAmpsAllTWCs still apply. Both are optional; a value that is not a positive number is ignored.

## Frequently Asked Questions

### Why do only some people see vehicle VINs in the TWCManager interface?

This feature was only introduced in firmware version 4.5.3, which is found on TWCs manufactured from March 2018 until the end of 2019.

TWCs prior to this version will not respond to queries from TWCManager regarding the VIN of the vehicle connected. Unfortunately there is no sign that newer TWC firmwares are being installed by Tesla vehicles, and the TWCs themselves do not have internet access to upgrade themselves.

A Gen3 Wall Connector never reports the VIN; see [Which car is on a Gen3](../Gen3_Status.md#which-car-is-on-a-gen3).

### Why is it so hard to just stop a vehicle from charging?

Good question:

   * The first revision of the TWC protocol (for TWCs produced prior to and during early 2017) has a command which will cleanly stop charging Tesla vehicles.

   * For Version 2 TWCs produced after this time, there is no single approach which cleanly stops a Tesla vehicle from charging, without the vehicle itself wanting to stop charging. The options that exist are:

      * Ask the vehicle to stop charging with a command over Bluetooth or the Tesla API (obviously a Tesla-only approach)

      * Stop communicating with the TWC for 30 seconds or more. This method stops the connected vehicle from charging, and will resume charging once the communications resume, however doing this a number of times will cause the vehicle to give up and the only way to resume charging is to unplug and replug the vehicle.

      * Sending the Stop message to Slave TWCs. This method will cause charging to cease immediately by instructing the TWC to open its relay, causing the charger to de-energise. A lack of CAN bus communication with the vehicle means that whilst the vehicle will stop charging immediately, it will log a number of errors and refuse to re-start charging again until it is unplugged and re-plugged (and will show a red LED ring around the charger port).

         * This method does work however if the DIP 2 switch is set in the **Down** position, which represents the use of legacy charge mode, which does not use CAN bus communication with Tesla vehicles. The usefulness of this configuration is equivalent to the "Stop communicating with slaves" behaviour above - it allows a number of stops and starts before the vehicle marks the charger as bad and refuses to charge until it is unplugged and re-plugged.

So in summary, the lack of a reasonable approach to stopping charging from the TWC side makes a command to the car (over Bluetooth or the Tesla API) the clean way to stop it. The [Stop Charging Method](../Settings.md#stop-charging-method) setting chooses between these.
