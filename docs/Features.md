# TWCManager Features

## Introduction

If you're new to the TWCManager project, or if you're not new but want to know what we've been busy working on, here's a long list of features that TWCManager provides today

## EVSE Support

   * Gen2 Tesla Wall Connectors, controlled over their RS-485 load-sharing bus, up to three per TWCManager.
   * Gen3 Tesla Wall Connectors over RS-485, by emulating the Neurio energy meter they read (1.4.0), with or without Gen2 units (1.4.1). With the Wall Connectors registered in the Tesla app, TWCManager knows which car is charging on each (1.4.1).
   * Car-side control of unmanaged EVSEs -- Tesla UMC, third-party, etc. (1.4.0): TWCManager sets the car's own charge rate, and notices the car arriving, plugging in, charging and leaving without waking it (1.4.1).

## Vehicle Support

   * Start and stop charging, set the charge rate, set a charge limit for each policy and restore the car's own limit when it leaves, and wake the car. TWCManager can do this over Bluetooth (free and local, no Tesla account), the Tesla Fleet API, the Owner API (deprecated by Tesla) or Home Assistant, and always tries the free ways first to keep Fleet API costs down.
   * Live updates from TeslaMate or Fleet Telemetry, or reads over Bluetooth, the Tesla API or Home Assistant when TWCManager needs them. Billed Fleet API reads stay within a monthly budget you set (1.4.1).
   * Leaves a sleeping car alone: TWCManager checks whether it is awake without waking it, and holds a command that need not wake it until the car is awake (1.4.1).
   * A Vehicles page showing whether each car is home, how TWCManager reaches it, and the commands it is waiting to send (1.4.1).
   * Recognition of vehicle VINs reported by TWCs, tracking of the vehicles that have charged, and per-vehicle permission to charge.
   * Limited support for other vehicles through generic interactions

## Green Power Support

   * Support for a large array of Solar Inverters, allowing tracking of Generation and Consumption values (where supported) and the ability to charge your vehicle using the delta between the Generated and Consumed power, essentially charging your vehicle for free from the sun
   * Support for a number of Battery systems including Growatt and Tesla Powerwall, where charging can be controlled via appropriate SOC, Generation and Consumption values.
   * Integration with other projects including openWB for control of charging
   * Unable to find a module which works for your inverter? The URL and MQTT modules read values from any system that can publish them.

## Control of TWC Devices

   * Support for controlling your TWC devices through a number of interfaces including an in-built Web UI, RESTful API, HomeAssistant and MQTT interfaces.

## Technical Features

   * Tested and compatible across a wide range of Python versions, with full support for apt-based distributions (Debian, Ubuntu, Raspberry Pi OS) and with Docker packages for ease of deployment.
