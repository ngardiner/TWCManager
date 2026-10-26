# TWCManager

[![Stable Release](https://img.shields.io/badge/Stable_Release-1.4.0-blue)](https://github.com/ngardiner/TWCManager/releases/tag/v1.4.0)
[![Development Release](https://img.shields.io/badge/Devel_Release-1.4.1-green.svg)](https://github.com/ngardiner/TWCManager/tree/main)
![Build Status](https://github.com/ngardiner/TWCManager/actions/workflows/test_suite.yml/badge.svg)
![GitHub commits](https://img.shields.io/github/commit-activity/m/ngardiner/TWCManager)
![Docker Pulls](https://img.shields.io/docker/pulls/twcmanager/twcmanager.svg)
![Contributors](https://img.shields.io/github/contributors/ngardiner/TWCManager)

## Screenshots
![Screenshot](docs/screenshot.png)
![Screenshot](docs/screenshot4.png)

## How it works

TWCManager decides how fast your cars may charge, from your solar generation, your house's consumption, your schedule and your wiring limits, and then has your charging stations and cars follow that decision.

You can combine any of the following. Any one is enough to start with, and each one you add makes TWCManager work better. We recommend at least one way to **command** each car and one way to **hear from** it.

* **Charging stations (EVSEs)**
    * Gen2 Tesla Wall Connectors, over their RS-485 load-sharing bus. TWCManager takes the master's place and sets each Wall Connector's current.
    * Gen3 Tesla Wall Connectors, over RS-485, by emulating the Neurio energy meter they read (1.4.0). You can run Gen3 units without any Gen2 (1.4.1); they still need their own RS-485 adapter.
    * Car-side control of unmanaged EVSEs -- Tesla UMC, third-party, etc. (1.4.0). TWCManager sets the car's own charge rate instead.
* **Ways to command the car** (start and stop charging, set the charge rate and charge limit, wake it)
    * Bluetooth (TeslaBLE): free and local, for cars in range. No Tesla account needed.
    * Tesla Fleet API: Tesla bills each command, and you need the Vehicle Command proxy.
    * Tesla Owner API: free, but it commands only the cars Tesla lists with `command_signing: off`. It is deprecated by Tesla and may stop working without notice, so don't rely on it alone: pair Bluetooth or set up the Fleet API as well.
    * Home Assistant's Tesla Fleet integration, billed like the Fleet API.
* **Ways to hear from the car** (whether it is home, plugged in, charging or asleep)
    * TeslaMate or Fleet Telemetry: they report each change as it happens. TeslaMate normally reads cars through the Owner API, which is deprecated by Tesla and may stop working without notice; Fleet Telemetry doesn't depend on it.
    * Bluetooth, Home Assistant or the Tesla API, which TWCManager asks when it needs to. Tesla API reads are free on the Owner API (deprecated by Tesla; it may stop working without notice) and billed on the Fleet API.

What you go without if you leave one out:

* **No Wall Connector:** TWCManager still sets the charge rate of a car on a Tesla UMC or another brand of charging station through the car itself (the Tesla API; Bluetooth and Home Assistant from 1.4.1), and notices it arriving, plugging in, charging and leaving (1.4.1). Without a Wall Connector or Bluetooth, TWCManager can't learn where home is: set it on the Settings page.
* **No TeslaMate or Fleet Telemetry:** TWCManager checks on each car without waking it, and reads it only when something changes. On the Fleet API those reads cost about $0.01 a day per car, within a monthly budget you can set.
* **No way to command the car:** TWCManager can only set the current a Wall Connector offers. It can't set a charge limit, and it can stop a car only through the Wall Connector; on a Gen2 that means methods a Tesla tolerates only a few times per session (see [Stop Charging Method](docs/Settings.md#stop-charging-method)).

[Supported setups](docs/README.md#supported-setups) has more detail on each combination.

An energy source (EMS module), such as your solar inverter or energy meter, is optional:

* **Without an EMS:** Scheduled Charging, Charge Now and the Non-Scheduled charge rate all work, but there is no solar charging.
* **With an EMS:** Track Green Energy charges from your solar generation. If the EMS also reads your house's consumption, charging uses only the surplus, `maxAmpsAllowedFromGrid` can cap what charging draws from the grid, `subtractChargerLoad` keeps the cars' own charging from counting as house load, and TWCManager can notice a car starting or stopping on a charging station it doesn't control (1.4.1).

Read the full [list of features](docs/Features.md).

## Installation and Configuration

All installation and configuration documentation (Physical and Software) is contained in the documentation repository [here](docs/README.md)

## Interfaces

To enable TWCManager to gather the solar generation information that it requires to adjust the charging rate, we provide Interfaces to allow information to be retrieved (EMS Interfaces) and Interfaces to allow us to provide information to external systems to inform them of current charging output and number of cars currently charging.

### EVSE Controllers

| Platform                                 | Details                 |
| ---------------------------------------- | ----------------------- |
| [Gen2 TWCs](docs/modules/Interface_RS485.md) | Controls Gen2 Wall Connectors over an RS-485 adapter |
| [Gen3 TWCs](docs/modules/Controller_Gen3TWCs.md) | Controls Gen3 Wall Connectors over an RS-485 adapter, by emulating their Neurio meter (1.4.0) |

### Control Interfaces

| Platform                            | Details                 |
| ----------------------------------- | ----------------------- |
| [HTTPControl](docs/modules/Control_HTTP.md) | Allows control via embedded HTTP server (with RESTful API interface) |
| [MQTTControl](docs/modules/Control_MQTT.md) | Allows control via MQTT |
| [WebIPCControl](docs/modules/Control_WebIPC.md) | Allows control via external scripts (sysv IPC) |

### EMS Interfaces

EMS Interfaces read solar generation and/or consumption values from an external system. If you enable more than one, TWCManager adds up their generation figures and their consumption figures, so each should measure something different. Current EMS interfaces are:

| Platform                      | Status        | Details                 |
| ----------------------------- | ------------- | ----------------------- |
| Dutch SmartMeter | In Development | Supports DSMR Serial Protocol |
| [DSMR-reader](docs/modules/EMS_DSMRreader.md)      | Available | Supports DSM Reader via MQTT |
| [Efergy](docs/modules/EMS_Efergy.md)            | Available | Supports Efergy Pro Sensors |
| [Enphase](docs/modules/EMS_Enphase.md)          | Available | Supports Enphase Enlighten API |
| [Fronius Inverter](docs/modules/EMS_Fronius.md) | Available | Supports Fronius Inverter API |
| [Growatt](docs/modules/EMS_Growatt.md)          | Available | Support for Growatt Battery systems |
| [HomeAssistant](docs/modules/EMS_HASS.md)       | Available | Supports HomeAssistant sensors |
| [IoTaWatt](docs/modules/EMS_Iotawatt.md)       | Available | Supports Iotawatt outputs |
| [Kostal (Pico/Plenticore)](docs/modules/EMS_Kostal.md) | Available | Support for Kostal Inverters via ModBus |
| [Open Energy Monitor](docs/modules/EMS_EmonCMS.md) | Available | Support [Open Energy Monitor](https://openenergymonitor.org) integration |
| [openHAB](docs/modules/EMS_OpenHab.md)          | Available | Supports openHAB items |
| [OpenWeatherMap](docs/modules/EMS_OpenWeatherMap.md) | Available | Supports OpenWeatherMap forecast-based tracking where inverter EMS is not available. |
| [P1 Monitor](docs/modules/EMS_P1Monitor.md)     | Available | Supports P1 Monitor (Energy Monitoring tool for Pi) |
| [SmartMe](docs/modules/EMS_SmartMe.md)          | Available | Support for smart-me.com API |
| [SmartPi](docs/modules/EMS_SmartPi.md)          | Available | Support for SmartPi Smart Meter |
| [SolarEdge](docs/modules/EMS_SolarEdge.md)      | Available | Support for SolarEdge API |
| [SolarLog](docs/modules/EMS_SolarLog.md)        | Available | Supports SolarLog Base API |
| [Tesla Powerwall2](docs/modules/EMS_Powerwall2.md) | Available | Support for Tesla Powerwall 2 |
| [The Energy Detective](docs/modules/EMS_TED.md) | Available | Support for TED (The Energy Detective) |
| [URL](docs/modules/EMS_URL.md)  | Available | Support for URL based generation/consumption values (eg Node-RED) |
| [Volkszähler](docs/modules/EMS_Volkszahler.md)  | Available | Support for Volkszahler |

### Logging Interfaces

Logging interfaces allow recording of statistics and values such as Green Energy data, Slave TWC status and Voltage and Amperage data to various interfaces.

This can be useful if you would like to track charger activity and use it for graphing or statistics purposes.

| Module                                     | Details                       |
| ------------------------------------------ | --------------------------    |
| [Console](docs/modules/Logging_Console.md) | Print output to console       |
| [CSV](docs/modules/Logging_CSV.md)         | Log data to CSV files         |
| [Files](docs/modules/Logging_Files.md)     | Log data to a log files       |
| [MySQL](docs/modules/Logging_MySQL.md)     | Log data to a MySQL Database  |
| [Sentry](docs/modules/Logging_Sentry.md)   | Log data to a Sentry project  |
| [SQLite](docs/modules/Logging_SQLite.md)   | Log data to a SQLite Database |

### Status Interfaces

Status interfaces publish TWCManager status information to external systems. Current Status interfaces are:

| Platform                                     | Details                 |
| -------------------------------------------- | ----------------------- |
| [HomeAssistant] | Deprecated, please use the MQTT status with Home Assistant autodiscovery |
| [MQTT](docs/modules/Status_MQTT.md)          | Publishes MQTT topics to monitor TWCManager State |

### Vehicle Interfaces

| Platform    | Details                                  |
| ----------- | ---------------------------------------- |
| [TeslaAPI](docs/modules/Vehicle_TeslaAPI.md) | Commands and reads through the Tesla Fleet API, and the Owner API (deprecated by Tesla) |
| [TeslaBLE](docs/modules/Vehicle_TeslaBLE.md) | Commands and reads over Bluetooth Low Energy, free and local |
| [TeslaMate](docs/modules/Vehicle_TeslaMate.md) | Live telemetry from TeslaMate (which normally uses the deprecated Owner API), and Tesla API token sync |
| [Fleet Telemetry](docs/modules/Vehicle_FleetTelemetryMQTT.md) | Live telemetry from a Tesla Fleet Telemetry server over MQTT |
| [Home Assistant](docs/modules/Vehicle_HomeAssistant.md) | Commands and reads through Home Assistant's Tesla Fleet integration |

The [Vehicles page](docs/README.md#the-vehicles-page) shows every car TWCManager knows and what it is doing with each.

## Warnings

It is important to read and understand the following warnings before using this tool:

* Misuse of the protocol described in this software can direct a Tesla Wall Charger to supply more current to a car than the charger wiring was designed for.
* This will trip a circuit breaker or may start a fire in the unlikely event that the circuit breaker fails.
* This software was not written or designed with the benefit of information from Tesla and there is always a small possibility that some unforeseen aspect of its operation could damage a Tesla vehicle or a Tesla Wall Charger.
* All efforts have been made to avoid such damage and this software is in active use on the author's own vehicle and TWC.
* In short, USE THIS SOFTWARE AT YOUR OWN RISK.

## Limitations
* Due to hardware limitations, TWCManager will not work with Tesla's older High Power Wall Connectors (HPWCs) that were discontinued around April 2016.
* Gen3 Wall Connectors are controlled indirectly and do not report a VIN. See [Gen3 status](docs/Gen3_Status.md#limitations).

## Attribution
* Original TWCManager distribution by [cdragon](https://github.com/cdragon/TWCManager)
   * The effort by @cdragon cannot be understated, if you would like to read more about the intense hardware hacking that was done to make this project a reality, read this [thread](https://teslamotorsclub.com/tmc/threads/new-wall-connector-load-sharing-protocol.72830).
* Integrated improvements from [flodom's](https://github.com/flodorn/TWCManager) TWCManager fork. 
    * Support for multiple vehicles on multiple chargers
    * Support for publishing status to MQTT topics
