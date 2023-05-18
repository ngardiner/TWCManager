# DSMR-reader EMS Module

## Introduction

The [DSMR-reader](https://github.com/dsmrreader/dsmr-reader) EMS module allows fetching of Consumption and Production values from the DSMR-reader JSON MQTT messages.

## How it works

TWCManager will subscribe to the JSON Telegram MQTT topic of DSMR-reader (`dsmr/json` by default, configure this at `/admin/dsmr_mqtt/jsontelegrammqttsettings/` of your DSMR-reader).

Comsumption is taken from the `electricity_currently_delivered` DSMR-reader value.

Production is taken from the `electricity_currently_returned` DSMR-reader value.

### Dependencies

DSMR-reader needs to publish the JSON Telegram messages to an MQTT broker.

### Note

Given that DSMR-reader measures the total household consumption, this includes the TWC. As a result, the TWC's load will be included in the  Consumption via the P1 output of the smart power meter. The smart meter does not know the total PV power delivery, just how much power is being delivered back to the grid. Please ensure the following configuration settings are enabled in your `config.json` file:

```
{
    "config": {
        "subtractChargerLoad": true,
        "treatGenerationAsGridDelivery": true,
    }
}
```

### Status

| Detail          | Value                          |
| --------------- | ------------------------------ |
| **Module Name** | DSMRreader                     |
| **Module Type** | Energy Management System (EMS) |
| **Features**    | Consumption, Production        |
| **Status**      | In Development                 |

## Configuration

The following table shows the available configuration parameters for the P1 Monitor EMS module.

| Parameter   | Value         |
| ----------- | ------------- |
| brokerIP    | *required* The IP address of the MQTT broker. |
| brokerPort  | *optional* The port of the MQTT broker. |
| username    | *optional* The username for the MQTT broker. |
| password    | *optional* The password for the MQTT broker. |
| topic       | *optional* The MQTT topic where the JSON messages will be published. |

### JSON Configuration Example

```
{
    "sources":{
        "DSMRreader": {
            "brokerIP": "192.168.1.2",
            "brokerPort": 1883,
            "username": "mqttuser",
            "password": "mqttpass",
            "topic": "dsmr/json",
        }
    }
}
```
