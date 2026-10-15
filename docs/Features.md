# TWCManager Features

## Introduction

If you're new to the TWCManager project, or if you're not new but want to know what we've been busy working on, here's a long list of features that TWCManager provides today

## Vehicle Support

   * Full support for Tesla vehicles including the ability to Start/Stop charging via API, and to recognize vehicle VINs.
   * Ability to track vehicles which have used Sub-TWC devices to charge, and to specify whether the vehicle has permissions to charge
   * Limited support for other vehicles through generic interactions

## Green Power Support

   * Support for a large array of Solar Inverters, allowing tracking of Generation and Consumption values (where supported) and the ability to charge your vehicle using the delta between the Generated and Consumed power, essentially charging your vehicle for free from the sun
   * Support for a number of Battery systems including Growatt and Tesla Powerwall, where charging can be controlled via appropriate SOC, Generation and Consumption values.
   * Integration with other projects including openWB for control of charging
   * Unable to find a module which works for your inverter or have an inverter which

## Control of TWC Devices

   * Support for controlling your TWC devices through a number of interfaces including an in-built Web UI, RESTful API, HomeAssistant and MQTT interfaces.

## Dynamic Pricing Support

   * Pricing modules for Amber Electric (Australian wholesale pricing), aWATTar (Austrian/German spot market) and the Spanish PVPC regulated tariff, plus statically configured prices for fixed-rate plans
   * Charging policy can react to import/export price levels, including negative wholesale prices, using the `getImportPrice()` and `getExportPrice()` policy values
   * Price forecasts from providers which supply them can be used to automatically identify the cheapest contiguous charging window via `getCheapestWindow()`
   * Spike detection (Amber Electric) exposes wholesale price spike events to charging policy
   * Current prices are published to MQTT topics (with Home Assistant discovery), exposed via the REST API, and displayed on the web dashboard

## Technical Features

   * Tested and compatible across a wide range of Python versions, with full support for apt-based distributions (Debian, Ubuntu, Raspberry Pi OS) and with Docker packages for ease of deployment.
