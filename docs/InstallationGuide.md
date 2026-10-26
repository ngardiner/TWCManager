# Installation Guide

This guide covers wiring a Gen2 Tesla Wall Connector to TWCManager over RS-485. A Gen3 Wall Connector is wired to its Neurio meter terminals instead; see [Gen3TWCs](modules/Controller_Gen3TWCs.md). A car-only install, where TWCManager controls cars over Bluetooth or the Tesla API, needs no wiring at all (see [Supported setups](README.md#supported-setups)).

## Before You Begin

The following steps will involve opening your Tesla Wall Charger (TWC) chassis cover for the purpose of connecting RS-485 wires to a number of terminals on the TWC. These terminals are adjacent to live components within the Tesla Wall Charger, and the risk of electrocution if appropriate safety steps are not taken is **high**.

The installation does not require a great amount of electrical skill or competence, but it does involve serious voltages and currents, and for that reason, the guide will stress repeatedly the proper implementation of safety process throughout. The last thing I would want is for anyone to harm following this tutorial and for that reason I would stress that **if you are not confident that you understand the risks involved and could take the necessary steps throughout this process to protect yourself from electrocution, please do not attempt this, please ask your electrician for assistance**.

  * If you are not confident or comfortable with the modification of the TWC installation, please stop here.
  * If you do not have access to a (or several) isolation devices to entirely de-energise the TWC during the installation, please stop here.
  * Remember that you can always ask your electrician to run these wires (safely) for you.

## Basic Installation

The basic installation approach consists of a pair of wires being installed into the RS-485 sockets of the Tesla Wall Charger, with an external device providing an RS-485 interface on which TWCManager communicates with the TWC to limit the charge based on the current Generation and Consumption rates of your solar installation.

**Pros**
  * Simpler installation, only requires feeding some wires through an existing entry point into the TWC.
  * Significantly less risk, as the advanced connection relies on storing a Raspberry Pi controller inside of the TWC and powering it from the LED display on the charger.

**Cons**
  * There is some visible external egress of these wires to connect to the controlling device.
     * This can be mitigated through running the cables through wall cavities, however for the sake of simplicity, this document will only deal with running the wires through visibly.

### Tools Required

You will need:

  * Some wire - 22 AWG guage wire is recommended.
  * An RS-485 interface. The easiest way to achieve this is a USB to RS-485 adapter. If using this adapter, I can recommend a USB-RS485-WE-1800-BT adapter, which both my and cdragon's installations have used. This has a FTDI chip and has been extremely stable in my testing.
  * A computer to run TWCManager on. A Raspberry Pi Zero W is recommended, as it has a low energy consumption. 
  * TT10 and TT20 "security" Torx Screw bits. The Tesla Wall Connector comes with a set of these torx bits included, it is in a package with some rubber grommets for the Wall Connector:
  
![Torx Screws](torxscrews.jpg)

### Remove the Cover

The first step is to remove the silver/grey TWC charger cover. This is held in by a Torx TT10 (the smaller bit) screw at the bottom of the unit. Loosen this screw and remove it carefully, as there are no spares provided with the unit. Be careful! This component is quite brittle. My electrician managed to crack mine when applying a little too much force.

![TWC Cover](twccover.png)

### Opening the Charger

Behind the TWC cover is another cover, which isolates all of the live components from the user. Once you open this cover, the wires covering live voltage will be exposed, so please **ensure the unit is isolated** at this point. This cover is held in place with Torx TT20 screws (the larger bit). **It is recommended that you be very careful removing these screws** as there are no spares provided, and they have a special coating on the back which acts as a waterproof gasket to ensure no water ingress to the unit. This is especially important in an outdoor installation.

![TWC Internal Cover](twcinternalcover.png)

### RS-485 Connection

Once you have removed the Tesla Wall Charger cover, you should see a set of 4 headers for RS-485 communication, in the centre of the unit. The header is highlighted in the following picture. Note that in the picture, one of the two RS-485 wires has already been attached.

![RS-485 Header Location](interface.jpg)

You may connect your RS-485 wires to either the In or the Out (but not both) header pins. To do this, you'll need a 3mm flat-head screwdriver. Strip approximately 3mm of insulation from the wires that you will be connecting, and feed them from the bottom of the RS-485 headers into the positive or negative terminals for either the In or Out headers.

Generally, the wires that you connect will be the following colours:

  * Positive: Orange or Red
  * Negative: Yellow or Black
  
Please check carefully with the documentation for your RS-485 interface. If you get this wiring wrong, it's possible that you'll feed power to the RS-485 header, potentially damaging the TWC.

### Rotary Switch

Identify the Rotary Switch, which is to the left of the RS-485 header. 

![Rotary Switch Location](rotary-switch.png)

This switch controls the amperage of the Tesla Wall Charger. Note down the original setting for your TWC. This will need to be changed to location F, in order to set the TWC to Slave Mode.

#### Europe / Oceania

| Rotary Switch | Maximum Output Current |
| ------------- | ---------------------- |
| 0             | Test Mode              |
| 1             | 6A                     |
| 2             | 8A                     |
| 3             | 10A                    |
| 4             | 13A                    |
| 5             | 16A                    |
| 6             | 20A                    |
| 7             | 25A                    |
| 8             | 32A                    |
| F             | Slave Mode             |

#### North America

| Rotary Switch | Maximum Output Current |
| ------------- | ---------------------- |
| 0             | Test Mode              |
| 1             | 12A                    |
| 2             | 16A                    |
| 3             | 20A                    |
| 4             | 24A                    |
| 5             | 28A                    |
| 6             | 32A                    |
| 7             | 36A                    |
| 8             | 40A                    |
| 9             | 48A                    |
| A             | 56A                    |
| B             | 64A                    |
| C             | 72A                    |
| D             | 80A                    |
| E             | Not valid              |
| F             | Slave Mode             |

## Expert Installation

The Expert Installation approach is not recommended for most users due to the
inherent difficulty of the installation process.  However, if you are
comfortable with cutting and soldering wires, the process is not overly
complicated.

Full details are [here](TWCManager%20Installation.pdf), but the process consists
of building an adapter cable to power the Raspberry Pi (5V) from the TWC's own
internal low-voltage components.

The following pieces are required:

- Heat shrink
- [UBEC 5V buck converter](https://www.adafruit.com/product/1385)
- [Male](https://www.digikey.com/product-detail/en/3m/4610-6051/MPK10K-ND/138305) and
  [female](https://www.digikey.com/product-detail/en/3m/89110-0101/MKC10A-ND/229690)
  IDC connectors, with a
  [ribbon cable](https://www.digikey.com/product-detail/en/3m/3365-10-300SF/MC10G-5-ND/145432)
  to connect them
- Optionally, a spare micro-USB cable

The TWC provides 14V low voltage to the front panel LEDs; this is the connector
you unplug to remove the inner cover.  The IDC connectors allow tapping this 14V
source, and the buck converter steps the 14V source down to 5V to power the
Raspberry Pi.

The output of the UBEC can be soldered directly to the Raspberry Pi's GPIO board
if you are very handy with fine soldering.  Alternatively, cut off the micro-USB
end of your spare cable, solder the UBEC's output to the red and black wires,
and power the Pi via USB as usual.

Use heat shrink to cover all exposed metal to reduce the risk of inadvertent
shorts or contact with the TWC's high-voltage components.

## Frequently Asked Questions

### How many Gen2 Wall Connectors can one TWCManager control?

The TWC Load Balancing protocol allows for up to four units within a group. As we are occupying one of the TWC Load Balancing unit IDs in order to provide the Master control of the charger group, there may be a total of three other units connected. When connecting additional units, they should be chained from the unused (In or Out) RS-485 terminals of the unit that is currently connected to TWCManager, which will allow all (up to) three of the units to be managed by one TWCManager instance.

Gen3 Wall Connectors don't use this protocol; several can share one Neurio bus (see [Multiple Gen3 units](Gen3_Status.md#multiple-gen3-units)).

### What can I do if my TWC is showing a red light blinking on the front of the unit?

This is because it has identified an error. If this occurred after starting the TWCManager.py script, it is highly likely that it has been caused by the TWCManager script.

Check the output of the TWCManager.py script. This will show you the reason for the error if it has been detected by the script. For example, if your rotary switch has not been adjusted to make the TWC a slave unit, you will see the following warning:

```
03:38:12 ERROR: TWC is set to Master mode so it can't be controlled by TWCManager.  Search installation instruction PDF for 'rotary switch' and set switch so its arrow points to F on the dial.
```

Similarly, if you are not running the TWCManager.py script and your TWC is set to Slave Mode, the same error condition will be shown via the TWC blinking red LED. In both cases, the error code is green: solid and red: 4 blinks. If you have any other error condition shown, refer to the table in your TWC user guide for specific details.

### Which Tesla Wall Connectors work with TWCManager?

   * The Tesla HPWC (Gen 1) wall charger was released in 2012 and was sold up until 2016, and can be identified by the LED. The Gen 1 used a bank of 4 DIP switches to configure the supplied amperage, rather than the rotary switch, and does not feature an RS458 bus at all. Gen 1 HPWC chargers are not capable of load sharing and cannot be used with TWCManager.
   * The Tesla TWC (Gen 2) wall charger was released in 2016 and was sold up until 2020, and differs only slighly from the Gen 1 from a visual perspective. The Gen 2 TWC provided better thermal management for the charger cable (an issue on older HPWCs) and better outdoor enclosure sealing, and introduced the charger load sharing protocol that this project uses.
      * Some, but not all Gen 2 Wall Connectors are able to charge non-Tesla vehicles, only the early versions have this capability. These can be identified by checking the part number 'P(TPN)' of the unit which is printed on the side. Part numbers ending in -00-x to -02-x (x can be any letter) should be able to charge non-Tesla vehicles.
      * Note, based on limited testing, the 'Legacy' DIP switch doesn't appear to affect whether the TWC can charge non-Tesla vehicles. When in SW-CAN (non Legacy) mode the TWC tries to communicate with the vehicle, and then drops back into legacy mode and starts charging if it doesn't get a response. If the Legacy DIP switch is on, then the handshake process is quicker for non-Tesla vehicles.
   * The TWC Gen 3 wall charger was released in January 2020 and is still being sold today, and can be identified by the white faceplate and the shorter charging cable. The Gen 3 TWC has a single RS485 header (whereas the Gen 2 has a double RS485 header) and connects to WiFi. TWCManager supports Gen3 units (1.4.0) via Neurio Modbus emulation, wired to their Neurio meter terminals rather than as described in this guide - see [Gen3_Status.md](Gen3_Status.md) and [modules/Controller_Gen3TWCs.md](modules/Controller_Gen3TWCs.md) for details.
