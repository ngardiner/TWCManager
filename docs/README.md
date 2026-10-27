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

   * In addition, we have a set of [Configuration Examples](config_examples.md) intended to help with more complex setups such as export-limited solar installations. These examples help to share information that has been discussed previously when setting up more complex charging policy.

## Gen3 Wall Connector

TWCManager supports Tesla Wall Connector Generation 3 units (1.4.0) via Neurio Modbus emulation, with or without Gen2 units (Gen3 without Gen2, 1.4.1).

   * [Gen3 Status and Architecture](Gen3_Status.md) - how Gen3 control works and its limitations
   * [Controller_Gen3TWCs module reference](modules/Controller_Gen3TWCs.md) - configuration guide

## Developing for TWCManager

Your contributions are most welcome! If you've been working on a new EMS module or you want to contribute to the project in any way, please take a look at our [Development Guide](DevelopmentGuide.md) and feel free to get involved!

## Frequently Asked Questions

### My car doesn't show up on the Vehicles page. How do I add it?

TWCManager learns about a car when a Gen2 Wall Connector reports its VIN, the Tesla energy site names it on a Gen3, or a Tesla API token, TeslaMate, Fleet Telemetry or Home Assistant lists it. If none of those applies, for example you use Bluetooth only and your Wall Connector doesn't report VINs, enter the car's VIN under **Add vehicle by VIN** on the Vehicles page (1.4.1). Then open the car and pair it over Bluetooth. A car you added this way that TWCManager has never seen otherwise can be removed again with **Forget this vehicle** on its page.

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

### Why doesn't charging stop when solar generation drops below the minimum?

TWCManager stops a car cleanly by sending the car itself a stop command: the default [Stop Charging Method](Settings.md#stop-charging-method). It sends it over Bluetooth (TeslaBLE) where it can, and otherwise through the Tesla API or Home Assistant (see [Supported setups](#supported-setups)). The stop goes only to cars that are home; how TWCManager decides that is described under Charge Limits in [Policy Customization](PolicyCustomization.md#charge-limits).

Without a way to command the car:

   * On a Gen2 Wall Connector, the car keeps charging at the minimum (usually 6A) unless you choose a Stop Charging Method that stops the Wall Connector itself. A Tesla tolerates that only a few times per session; see [Stopping a car on a Gen2](modules/Interface_RS485.md#why-is-it-so-hard-to-just-stop-a-vehicle-from-charging).
   * On a Gen3 Wall Connector, TWCManager stops the unit by leaving it no headroom, which works only while no other Gen3 unit on the same bus is charging (see [Gen3 limitations](Gen3_Status.md#limitations)).
   * On a charging station TWCManager doesn't control, it can't stop the car.

### Do I need a Tesla API token?

   * Not necessarily. TWCManager needs a way to command a car to stop it cleanly, set its charge limit or wake it. Bluetooth (TeslaBLE) is one, and needs no token; a Tesla API token or Home Assistant gives another.
   * For cloud commands, register your own Fleet API app (see the [Tesla Fleet API setup guide](modules/Vehicle_TeslaAPI.md)), sync a token from TeslaMate, or paste one on the Settings page. An Owner API token commands only cars Tesla lists with `command_signing: off`, and the Owner API is deprecated by Tesla and may stop working without notice (see [Owner API tokens](modules/Vehicle_TeslaAPI.md#owner-api-tokens)).
