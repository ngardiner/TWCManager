# TeslaMate Vehicle Integration

## Introduction

<a href="https://github.com/adriankumpf/teslamate">TeslaMate</a> is a self-hosted Tesla datalogger.

The purpose of this integration is to allow users of TWCManager who also use <a href="https://github.com/adriankumpf/teslamate">TeslaMate</a> to leverage the work already performed by TeslaMate to fetch vehicle status, significantly reducing the number of Tesla API calls that TWCManager needs to make.

With both the API Token and Telemetry options enabled, TeslaMate manages the Tesla API login and reports each car's changes. While its feed is live for a car, TWCManager sends the Tesla API only the call that lists the cars on your account, the commands it needs (such as wake_up or charge), and the reads a command or a TWC's plug event asks for. While the feed is down, or TeslaMate reports a car unhealthy, the vehicle watch reads that car as it would any other.

TeslaMate normally reads your cars through the Tesla Owner API, so it carries the same caveat as using the Owner API directly: Tesla has deprecated it, and it may stop working without notice. [Fleet Telemetry](Vehicle_FleetTelemetryMQTT.md) is the live feed that doesn't depend on it.

TeslaMate is a free, live telemetry source: it reports each change as it happens. It sends no commands; for those, TWCManager needs a command path such as [TeslaBLE](Vehicle_TeslaBLE.md) or the [Tesla API](Vehicle_TeslaAPI.md).

There is no requirement to use both sync functions when integrating with TeslaMate. You can sync only tokens, or only telemetry.

## Integrations

### Tesla API Tokens

TWCManager can synchronize the TeslaMate API tokens from the TeslaMate database. This allows token management to be performed on our behalf by TeslaMate rather than within TWCManager.

This is performed through direct queries of the TeslaMate database, and is not used by the telemetry feature of the TeslaMate module, which is separate. You do not need to use the API Token sync function even if you do use the telemtry sync function.

Please note that turning on the Token Sync functionality of this module will have the following affect on TWCManager's Tesla API token handling functionality:

   * Sync copies TeslaMate's access token only, into the Owner API or Fleet API slot according to the token's type. TeslaMate's refresh token is never read.
   * Sync never replaces a token you pasted or logged in with; it fills a slot only while that slot is empty or holds a synced token.
   * TWCManager does not refresh synced tokens. A synced token is used until it expires, and the next sync after TeslaMate refreshes it replaces it.
   * You can still log in with the Fleet API while sync is on; that token is kept beside the synced one.

Please note that since TeslaMate v1.27.0, TeslaMate performs encryption of Tesla API tokens. You will need to specify your encryption key in the configuration to continue to fetch API keys.

#### Configuration

```
   "vehicle": {
       "TeslaMate": {
           "enabled": true,
           "syncTokens": true,
           "db_host": "192.168.1.1",
           "db_name": "teslamate",
           "db_pass": "teslamate",
           "db_user": "teslamate",
           "encryption_key": "changeme"
       }
   }
```

#### Access to Database Instance

By default, TWCManager may not be able to access the database for TeslaMate.

Depending on your TeslaMate installation type, you may need to take different steps to make the PostgreSQL database available to TWCManager in order to sync Tesla auth tokens. Follow the appropriate section below for your installation type.

##### Manual Installation

For a manual installation, the ```/etc/postgresql/pg_hba.conf``` file will need to be updated to allow the TWCManager host to connect remotely to the database.

Add the following line below to pg_hba.conf, substituting the IP address of your TWCManager machine:

```
host    teslamate       teslamate       192.168.1.1/32        md5
```

After adding this, reload the PostgreSQL configuration:

```
service postgresql reload
```

##### Docker Installation

To be updated - I am not using this configuration, I welcome feedback if you do.

### Telemetry Information

TWCManager can use the MQTT topics advertised by TeslaMate to recieve charge state and SoC information from your vehicle

#### Confirming Telemetry Flow

Once you have MQTT connectivity set up for TeslaMate telemetry, you should see the following log entry appear for each of the vehicles tracked by TeslaMate that TWCManager can match to a VIN (from the TeslaMate database when the database settings are configured, otherwise by the car's name), which indicates that we have detected Telemetry information for this vehicle:

```
00:06:58 🚗 TeslaMat 20 Vehicle *display_name* telemetry being provided by TeslaMateVehicle
```

While TeslaMate's feed is live for a car, TWCManager takes that car's changes (plug-ins, drives, position, sleep) from the feed, and the [vehicle watch](../Settings.md#vehicle-watch) leaves the car alone.

The feed counts as live for a car while TWCManager is connected to the MQTT broker and TeslaMate's `healthy` topic for that car, once published, says `true`. When TeslaMate reports a car unhealthy (it has stopped tracking the car), TWCManager logs:

```
TeslaMate reports car <id> unhealthy
```

and the vehicle watch looks after the car, at its next due check, until TeslaMate reports it healthy again. The same happens for every car while the broker connection is down; TWCManager logs `Telemetry from TeslaMateVehicle has resumed` once it reconnects.

#### Configuration

```
   "vehicle": {
       "TeslaMate": {
           "enabled": true,
           "syncTelemetry": true,
           "mqtt_host": "192.168.1.1",
           "mqtt_user": "teslamate",
           "mqtt_pass": "teslamate",
           "mqtt_prefix": "teslamate"
       }
   }
```

To sync tokens as well, add the database settings shown under Tesla API Tokens above.
