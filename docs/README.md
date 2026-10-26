# TWCManager Documentation

## Having Trouble?

If you're having trouble getting TWCManager working, check out our [Troubleshooting Guide](Troubleshooting.md) to see if any of our tips help you out!

## Supported setups

You can combine any charging stations (EVSEs) and car connections below. Any one is enough to start with, and each one you add makes TWCManager work better. We recommend at least one way to **command** each car and one way to **hear from** it.

### Charging stations (EVSEs)

| EVSE | What you need | Guide |
| ---- | ------------- | ----- |
| Gen2 Tesla Wall Connector | An RS-485 adapter wired to the Wall Connector's load-sharing terminals, with its rotary switch set to F. TWCManager takes the master's place and sets each unit's current. Units with firmware 4.5.3 or later also report which car is plugged in. | [Installation Guide](InstallationGuide.md), [RS485](modules/Interface_RS485.md) |
| Gen3 Tesla Wall Connector, over RS-485 (1.4.0) | An RS-485 adapter wired to the Wall Connector's Neurio meter terminals, with the Wall Connector reachable on your network. TWCManager sets each unit's current by emulating the Neurio meter. To know which car is charging on which unit, TWCManager also needs the units registered in the Tesla app and a Fleet API token with the `energy_device_data` scope (1.4.1); see [Gen3 status](Gen3_Status.md#which-car-is-on-a-gen3). | [Gen3 status](Gen3_Status.md), [Gen3TWCs](modules/Controller_Gen3TWCs.md) |
| Car-side control of unmanaged EVSEs -- Tesla UMC, third-party, etc. (1.4.0) | A way to command the car (below). TWCManager sets the car's own charge rate, and notices the car arriving, plugging in, charging and leaving (1.4.1). | [Vehicle watch](Settings.md#vehicle-watch) |

You can mix Gen2 and Gen3 units, use either one alone, or use neither (1.4.1 for an install without Gen2). Gen2 and Gen3 each need their own RS-485 adapter, so a Gen3-only install still has one, wired to the Gen3's Neurio terminals. Without a Gen2 adapter, leave the `interface` settings as shipped: TWCManager logs once that it can't open the adapter and carries on.

### Ways to command the car

These start and stop charging, set the charge rate and charge limit, and wake the car. TWCManager always tries the free ones first (the Wall Connector itself, then Bluetooth) and uses a billed one only when no free one can do the job. The [Vehicle Command Policy](Settings.md#vehicle-command-policy) on the Settings page lets you rule some out, for example to never pay for a command.

| Path | Cost | What you need | Guide |
| ---- | ---- | ------------- | ----- |
| Bluetooth (TeslaBLE) | Free, local | The car within Bluetooth range of the TWCManager host, and a key paired with it. No Tesla account. | [TeslaBLE](modules/Vehicle_TeslaBLE.md) |
| Tesla Fleet API | Billed per command and wake | Your own app registered with Tesla, and the Vehicle Command proxy. | [Tesla Fleet API](modules/Vehicle_TeslaAPI.md) |
| Tesla Owner API | Free | An Owner API token (for example from TeslaMate). It commands only cars Tesla lists with `command_signing: off`, seen on older Model S and X. Deprecated by Tesla; it may stop working without notice, so pair Bluetooth or set up the Fleet API as well. | [Owner API tokens](modules/Vehicle_TeslaAPI.md#owner-api-tokens) |
| Home Assistant | Billed like the Fleet API; used only when the Fleet API can't be | Home Assistant's Tesla Fleet integration. It can't wake the car. | [Home Assistant](modules/Vehicle_HomeAssistant.md) |

### Ways to hear from the car

These tell TWCManager whether each car is home, plugged in, charging, awake or asleep.

| Source | How | Cost | Guide |
| ------ | --- | ---- | ----- |
| TeslaMate | Reports each change as it happens. TeslaMate normally reads cars through the Owner API, which is deprecated by Tesla and may stop working without notice; Fleet Telemetry doesn't depend on it. | Free | [TeslaMate](modules/Vehicle_TeslaMate.md) |
| Fleet Telemetry | Reports each change as it happens | Tesla bills the data the car sends | [Fleet Telemetry](modules/Vehicle_FleetTelemetryMQTT.md) |
| Bluetooth | TWCManager asks when it needs to; a car in range answers even asleep, without waking | Free, local | [TeslaBLE](modules/Vehicle_TeslaBLE.md) |
| Home Assistant | TWCManager asks when it needs to | Free | [Home Assistant](modules/Vehicle_HomeAssistant.md) |
| Tesla API | TWCManager asks when it needs to | Free on the Owner API (deprecated by Tesla; it may stop working without notice, so prefer Fleet Telemetry), billed on the Fleet API | [Tesla Fleet API](modules/Vehicle_TeslaAPI.md) |

### What you go without

| If you have no... | TWCManager still... | But you lose |
| ----------------- | ------------------- | ------------ |
| Wall Connector | Sets the charge rate of a car on a Tesla UMC or another charging station through the car itself (Bluetooth and Home Assistant can do this from 1.4.1), and notices it arriving, plugging in, charging and leaving (1.4.1). | Control of any car it can't command. Without a Wall Connector or Bluetooth, TWCManager can't learn where home is: set it on the Settings page, or no car is treated as home. |
| TeslaMate or Fleet Telemetry | Checks on each car without waking it, and reads it when something changes. A Wall Connector reports its own car plugging in and charging. | Instant news of changes away from a Wall Connector. On the Fleet API, reads cost about $0.01 a day per car, within the `watch` budget ([Settings](Settings.md#budget)). |
| Way to command the car | Sets the current each Wall Connector offers. | Charge limits, waking the car, and a clean stop. A car can be stopped only through its Wall Connector; on a Gen2, by methods a Tesla tolerates only a few times per session (see [Stop Charging Method](Settings.md#stop-charging-method)). |
| Bluetooth | Commands and reads the car through the cloud. | Free local commands, and knowing the car is home because it is in range. Commands it would have sent may be billed on the Fleet API instead. |

### Energy sources (EMS)

An EMS module reads your solar inverter or energy meter. It is optional.

* **Without an EMS:** Scheduled Charging, Charge Now and the Non-Scheduled charge rate all work, but there is no solar charging.
* **With an EMS:** Track Green Energy charges from your solar generation. If the EMS also reads your house's consumption:
   * charging uses only the surplus your house doesn't use;
   * `maxAmpsAllowedFromGrid` in config.json can cap what charging draws from the grid outside solar charging;
   * if your meter counts the cars' charging too, `subtractChargerLoad` in config.json keeps it from counting as house load, for Gen2 and Gen3 Wall Connectors and cars on other charging stations alike;
   * TWCManager notices a car starting or stopping on a charging station it doesn't control, from the jump in your house's load (1.4.1; `emsTrigger` under [Vehicle watch](Settings.md#consumption-step)).

[Consumption Offsets](Settings.md#consumption-offsets) can stand in for a consumption reading you don't have. The EMS modules are listed in the [main README](../README.md#ems-interfaces).

## The Vehicles page

The web interface's Vehicles page lists every car TWCManager knows, with:

* whether it is Home, Probably home (for example, driving and not yet seen elsewhere) or Away (1.4.1);
* how TWCManager reaches it (Bluetooth, the Tesla API, Home Assistant, a Wall Connector);
* what TWCManager last checked on it and when it checks next (1.4.1);
* when it was last seen, and its own charge limit.

Open a car to pair it over Bluetooth (Pair with Vehicle), scan for it (Scan for vehicle, where a scan can run; otherwise the reason it can't), see commands TWCManager is waiting to send and why (1.4.1), and see any problem with its Fleet Telemetry configuration.

How TWCManager decides whether a car is home, and learns where home is, is described under Charge Limits in [Policy Customization](PolicyCustomization.md#charge-limits) and Home Location in [Settings](Settings.md#home-location).

## Physical (RS-485) Installation

Please see the [Installation Guide](InstallationGuide.md) for detailed information on wiring a Gen2 Tesla Wall Connector to TWCManager. For a Gen3, see [Gen3TWCs](modules/Controller_Gen3TWCs.md). A car-only install needs no wiring.

## Software Installation

The following options exist for installing TWCManager:

   * [Docker](Software_Docker.md) - Docker uses containers to run applications in their own isolated environment. The benefits of docker include simple upgrading easy portability, with significantly less worries about Python versions or permissions in a Dockere environment than a Manual environment.

   * [Manual](Software_Manual.md) - A manual installation involves downloading the source code for TWCManager from GitHub, installing dependencies and then running TWCManager as a service.

### Configuration

The TWCManager configuration file at ```/etc/twcmanager/config.json``` has lots of information about each of the configuration parameters and their effect on your installation. We highly recommend reading through the file carefully as you configure your installation, as this resolves the majority of issues.

   * In addition, we have a set of [Configuration Examples](config_examples.md) intended to help with more complex setups such as those utilizing Flex Charging. These examples help to share information that has been discussed previously when setting up more complex charging policy.

## Gen3 Wall Connector

TWCManager supports Tesla Wall Connector Generation 3 units (1.4.0) via Neurio Modbus emulation, with or without Gen2 units (Gen3 without Gen2, 1.4.1).

   * [Gen3 Status and Architecture](Gen3_Status.md) - how Gen3 control works and its limitations
   * [Controller_Gen3TWCs module reference](modules/Controller_Gen3TWCs.md) - configuration guide

## Developing for TWCManager

Your contributions are most welcome! If you've been working on a new EMS module or you want to contribute to the project in any way, please take a look at our [Development Guide](DevelopmentGuide.md) and feel free to get involved!

## Frequently Asked Questions

### My car doesn't show up on the Vehicles page. How do I add it?

TWCManager learns about a car when a Gen2 Wall Connector reports its VIN, the Tesla energy site names it on a Gen3, or a Tesla API token, TeslaMate, Fleet Telemetry or Home Assistant lists it. If none of those applies, for example you use Bluetooth only and your Wall Connector doesn't report VINs, enter the car's VIN under **Add vehicle by VIN** on the Vehicles page (1.4.1). Then open the car and pair it over Bluetooth. A car you added this way that TWCManager has never seen otherwise can be removed again with **Forget this vehicle** on its page.

### How many units can be set up in this fashion?

The TWC Load Balancing protocol allows for up to four units within a group. As we are occupying one of the TWC Load Balancing unit IDs in order to provide the Master control of the charger group, there may be a total of three other units connected. When connecting additional units, they should be chained from the unused (In or Out) RS-485 terminals of the unit that is currently connected to TWCManager, which will allow all (up to) three of the units to be managed by one TWCManager instance.

### What can I do if my TWC is showing a red light blinking on the front of the unit?

This is because it has identified an error. If this occurred after starting the TWCManager.py script, it is highly likely that it has been caused by the TWCManager script.

Check the output of the TWCManager.py script. This will show you the reason for the error if it has been detected by the script. For example, if your rotary switch has not been adjusted to make the TWC a slave unit, you will see the following warning:

```
03:38:12 ERROR: TWC is set to Master mode so it can't be controlled by TWCManager.  Search installation instruction PDF for 'rotary switch' and set switch so its arrow points to F on the dial.
```

Similarly, if you are not running the TWCManager.py script and your TWC is set to Slave Mode, the same error condition will be shown via the TWC blinking red LED. In both cases, the error code is green: solid and red: 4 blinks. If you have any other error condition shown, refer to the table in your TWC user guide for specific details.

### Why is my car only charging at 6A?

There are a few reasons why you might see your car charging only at 6 amps:

  * There is less than 6A worth of generation capacity available
     * In this case, the charger will charge at 6A (2kW), as charging below this rate is of no benefit - it would not be sufficient to power the battery conditioning, and would be very inefficient as most of the power would be lost.
     
  * Your configuration has not been modified from the default
     * There are some safe defaults used in the config.json which ships with TWCManager. In particular:
        * wiringMaxAmpsAllTWCs: The maximum number of amps that all slave TWCs are able to draw simultaneously from the shared power source. Because a load-balanced TWC installation involves each of the TWCs sharing the same power feed, we need to configure a maximum allocation of current for all connected TWCs. This is set to 6A by default.
        * wiringMaxAmpsPerTWC: The maximum number of amps that each individual TWC is capable of drawing. If you think of the previous value (AllTWCs) as the capacity of the trunk power source that all TWCs are drawing from, this value (PerTWC) is the capability of each indiviual TWC, based on the wire gauge between the shared power source and the individual TWC. In a single TWC installation, this will be equal to the value of wiringMaxAmpsAllTWCs.
        
  * Conflict between Charger Consumption and Consumption Sensor
     * There are two ways in which your charger may be wired in an environment where you are able to access a consumption sensor:
        * The charger may be monitored as part of the consumption meter.
        * Your charger may not be monitored as part of the consumption meter.
     * If your charger is not monitored by the consumption meter, you do not need to make any changes to the default configuration. This configuration assumes that when you access consumption data, it does not count the charging load.
     * If your charging load/draw is counted by the consumption meter, this would cause the charger to be consistently forced down to 6A, as all of the generation would be canceled out by the power you draw to charge the vehicle.

### Why 6A?

   * 6A is the default minAmpsPerTWC setting. 
   * Charging at 240V 5A wastes 8.6% more energy than charging at 240V 10A.
      * Because of this, it is important to find a point at which you are comfortable setting a floor for charging current. If this is set too low, it will be inefficient. 

### Why doesn't charging stop when Solar generation drops below minimum

   * There are a few different ways in which we can stop a car from charging on a TWC:

      * V1 Chargers

The first revision of TWC chargers were able to stop charging by setting the maxAmps value to 0 amps. This is not effective for newer TWC chargers, however.

      * Stop communicating with the Slave TWCs. 
      
This is effective, in that stopping communications with a Slave TWC will stop the car from charging. Unfortunately the byproduct of this is that this can sometimes result in the car going into an error state, where charging can only be restarted by unplugging and replugging the TWC.

In an upcoming release, this will be offered as a switchable option to replace the use of Tesla API keys for those who are not comfortable with providing their credentials to the script.
      
      * Use the Tesla API to connect to the car, sending a command to stop/start charging.

This requires a Tesla API token, which is stored locally within a settings file. Tesla retired the legacy Owner API login, so cloud control now requires a registered FleetAPI app (see the [Tesla FleetAPI setup guide](modules/Vehicle_TeslaAPI.md)), TeslaMate token sync, or pasting a token into the Settings page. An Owner API token commands only vehicles Tesla reports with `command_signing: off`, and reads the others (see [Owner API tokens](modules/Vehicle_TeslaAPI.md#owner-api-tokens)). Local BLE control (TeslaBLE) is also available and needs no cloud token.

If you have multiple cars, TWCManager will attempt to identify which cars are home using geofencing. The following page of the TMC forums thread explains it better than I could: https://teslamotorsclub.com/tmc/threads/new-wall-connector-load-sharing-protocol.72830/page-16

### Why do I need a Tesla API token?

   * TWCManager uses a Tesla API token to talk to your vehicle(s).
   * When the available charger capacity falls below minAmpsPerTWC, the TWCManager script will contact the Tesla API to tell the vehicle to stop charging. If this is not configured, your vehicle will continue to charge at 6A even when the charging policy dictates that we stop charging.

### Why does my TWC increase charging momentarily to 21A or 17A around the time that it changes charging rates?

There are a number of reasons for this:

   * In the 2017.44 firmware version Tesla released around October 2017, a bug was introduced which led to vehicle charging rates falling to 6A when charging rates were raised. This is resolved by spiking the amps.

### Why is it so hard to just stop a vehicle from charging?

Good question:

   * Version 1 of the TWC protocol (for TWCs produced prior to and during early 2017) has a command which will cleanly stop charging Tesla vehicles.

   * For Version 2 TWCs produced after this time, there is no single approach which cleanly stops a Tesla vehicle from charging, without the vehicle itself wanting to stop charging. The options that exist are:

      * Ask the vehicle to stop charging by sending a message via the Tesla vehicle API (obviously a Tesla-only approach)

      * Stop communicating with the TWC for 30 seconds or more. This method stops the connected vehicle from charging, and will resume charging once the communications resume, however doing this a number of times will cause the vehicle to give up and the only way to resume charging is to unplug and replug the vehicle.

      * Sending the Stop message to Slave TWCs. This method will cause charging to cease immediately by instructing the TWC to open its relay, causing the charger to de-energise. A lack of CAN bus communication with the vehicle means that whilst the vehicle will stop charging immediately, it will log a number of errors and refuse to re-start charging again until it is unplugged and re-plugged (and will show a red LED ring around the charger port).

         * This method does work however if the DIP 2 switch is set in the **Down** position, which represents the use of legacy charge mode, which does not use CAN bus communication with Tesla vehicles. The usefulness of this configuration is equivalent to the "Stop communicating with slaves" behaviour above - it allows a number of stops and starts before the vehicle marks the charger as bad and refuses to charge until it is unplugged and re-plugged.

So in summary, the lack of a reasonable approach to stopping charging from the TWC side necessitates an API based solution.

### Why do only some people see vehicle VINs in the TWCManager interface?

This feature was only introduced in firmware version 4.5.3, which is found on TWCs manufactured from March 2018 until the end of 2019.

TWCs prior to this version will not respond to queries from TWCManager regarding the VIN of the vehicle connected. Unfortunately there is no sign that newer TWC firmwares are being installed by Tesla vehicles, and the TWCs themselves do not have internet access to upgrade themselves.

### What do we know about the various different Tesla Wall Charger revisions and how they operate?

   * The Tesla HPWC (Gen 1) wall charger was released in 2012 and was sold up until 2016, and can be identified by the LED. The Gen 1 used a bank of 4 DIP switches to configure the supplied amperage, rather than the rotary switch, and does not feature an RS458 bus at all. Gen 1 HPWC chargers are not capable of load sharing and cannot be used with TWCManager.
   * The Tesla TWC (Gen 2) wall charger was released in 2016 and was sold up until 2020, and differs only slighly from the Gen 1 from a visual perspective. The Gen 2 TWC provided better thermal management for the charger cable (an issue on older HPWCs) and better outdoor enclosure sealing, and introduced the charger load sharing protocol that this project uses.
      * Some, but not all Gen 2 Wall Connectors are able to charge non-Tesla vehicles, only the early versions have this capability. These can be identified by checking the part number 'P(TPN)' of the unit which is printed on the side. Part numbers ending in -00-x to -02-x (x can be any letter) should be able to charge non-Tesla vehicles.
      * Note, based on limited testing, the 'Legacy' DIP switch doesn't appear to affect whether the TWC can charge non-Tesla vehicles. When in SW-CAN (non Legacy) mode the TWC tries to communicate with the vehicle, and then drops back into legacy mode and starts charging if it doesn't get a response. If the Legacy DIP switch is on, then the handshake process is quicker for non-Tesla vehicles.
   * The TWC Gen 3 wall charger was released in January 2020 and is still being sold today, and can be identified by the white faceplate and the shorter charging cable. The Gen 3 TWC has a single RS485 header (whereas the Gen 2 has a double RS485 header) and connects to WiFi. As of v1.4.0, TWCManager supports Gen3 units via Neurio Modbus emulation - see [Gen3_Status.md](Gen3_Status.md) and [modules/Controller_Gen3TWCs.md](modules/Controller_Gen3TWCs.md) for details.
