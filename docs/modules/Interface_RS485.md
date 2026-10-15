# RS485 Communication Interface

## Introduction

The RS485 Interface Module provides Serial and (limited) Network connectivity between TWCManager and Slave TWCs.

You should use this module if:

   * Your device is directly connected to the TWC devices via a physical RS485 (2-wire) connection, either via USB device, shield, etc.
   * Your device connects over the network to an RS485 to TCP converter which is supported via the Raw or RFC2217 (Telnet) protocols without encryption.

## Serial

Serial communications with TWC is established through the use of a Serial Device, such as the default setting of ```/dev/ttyUSB0``` which represents the first USB to Serial device connected to a machine.

Some examples of Serial configuration include:

```"device": "/dev/ttyUSB0"``` - Connection to a USB to RS485 adaptor
```"device": "/dev/ttyS0"```   - Connection to an onboard Serial interface

## Network Communications

There are two different network protocols which are supported by the RS485 module. These are less configurable than the alternate planned TCP module, however the TCP module is not yet available for use.

   * rfc2217 - Telnet to Serial
   * socket  - Raw network to Serial

Generally, a device will provide documentation which describes the network encoding used. 

Examples of the configuration of these protocols:

```"device": "rfc2217://192.168.1.2:4000/"```

```"device": "socket://192.168.1.2:4000/"```

## Per-TWC amps range

By default every slave TWC is offered between minAmpsPerTWC (never less than the TWC itself supports) and its wiring maximum. An idle TWC is offered that minimum so it can see a car plug in. To set a different range for one TWC, add `controller.Gen2TWCs` to config.json:

```json
"controller.Gen2TWCs": {
    "ranges": {
        "8244": { "minAmps": 8, "maxAmps": 16 }
    }
}
```

The key is the TWC's ID as four hex digits, as the log and web page show it. `minAmps` replaces minAmpsPerTWC for that TWC, but never goes below what the TWC supports. `maxAmps` caps what the power distributor offers it; wiringMaxAmpsPerTWC and wiringMaxAmpsAllTWCs still apply. Both are optional; a value that is not a positive number is ignored.
